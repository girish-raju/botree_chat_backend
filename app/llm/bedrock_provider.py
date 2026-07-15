"""Amazon Bedrock-backed `LLMProvider` implementation (Converse API).

Talks to Bedrock's native Converse endpoints
(`https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse` and
`.../converse-stream`) over plain HTTP via `httpx`, authenticated with a
bearer token: either a Bedrock API key from settings, or one auto-generated
from plain IAM keys via `aws-bedrock-token-generator` (pure SigV4 presigning,
no network call).

Serves Claude models (e.g. `global.anthropic.claude-sonnet-5`). Claude's
adaptive thinking arrives as separate `reasoningContent` blocks in Converse
responses — only `text` blocks are ever surfaced, so chain-of-thought never
reaches the user. Streaming uses the AWS event-stream framing, decoded with
botocore's `EventStreamBuffer`.

Control flow (strict-JSON prompt, `parse_llm_json` repair, exactly one forced
retry, streaming with non-streaming fallback) mirrors `CloudflareProvider`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx
from aws_bedrock_token_generator import BedrockTokenGenerator
from botocore.credentials import Credentials
from botocore.eventstream import EventStreamBuffer

from app.config import Settings
from app.errors import UpstreamLLMError
from app.llm.base import SQLPlan, Turn, ValidateHook, parse_suggestion_list
from app.llm.cloudflare_provider import parse_llm_json
from app.llm.prompts import (
    ANSWER_PROMPT,
    REWRITE_PROMPT,
    STRICT_JSON_SQL_PROMPT,
    SUGGEST_FOLLOWUPS_PROMPT,
    TITLE_PROMPT,
    render_answer_facts,
    render_sample_rows,
)
from app.llm.usage import record_usage

_CONVERSE_URL = "https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse"
_CONVERSE_STREAM_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse-stream"
)

# Claude Sonnet's adaptive thinking tokens count against the same output
# budget as the answer, so leave headroom beyond what the strict-JSON plan
# itself needs.
_MAX_TOKENS = 4096

# Generated bearer tokens are valid 12h; refresh well before that.
_TOKEN_REFRESH_S = 6 * 3600


def _converse_messages(messages: list[dict]) -> list[dict]:
    """Convert `{role, content}` dicts to Converse shape, merging consecutive
    same-role turns (Converse requires strict user/assistant alternation)."""
    out: list[dict] = []
    for message in messages:
        block = {"text": message["content"]}
        if out and out[-1]["role"] == message["role"]:
            out[-1]["content"].append(block)
        else:
            out.append({"role": message["role"], "content": [block]})
    return out


def _history_to_messages(history: list[Turn]) -> list[dict]:
    return [{"role": turn.role, "content": turn.text} for turn in history]


class BedrockProvider:
    """`LLMProvider` implementation backed by Amazon Bedrock (Claude via Converse)."""

    name = "bedrock"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._own_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def _url(self) -> str:
        return _CONVERSE_URL.format(
            region=self._settings.bedrock_region, model=self._settings.bedrock_model
        )

    @property
    def _stream_url(self) -> str:
        return _CONVERSE_STREAM_URL.format(
            region=self._settings.bedrock_region, model=self._settings.bedrock_model
        )

    def _bearer_token(self) -> str:
        """Return a Bedrock bearer token.

        A configured `bedrock_api_key` wins; otherwise one is generated from
        the IAM keys (offline SigV4 presigning) and cached until refresh.
        """
        if self._settings.bedrock_api_key:
            return self._settings.bedrock_api_key
        if not (self._settings.aws_access_key_id and self._settings.aws_secret_access_key):
            raise UpstreamLLMError(
                "no Bedrock credentials configured: set BEDROCK_API_KEY or "
                "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in .env"
            )
        now = time.monotonic()
        if self._token is None or now >= self._token_expires_at:
            credentials = Credentials(
                self._settings.aws_access_key_id,
                self._settings.aws_secret_access_key,
            )
            self._token = BedrockTokenGenerator().get_token(
                credentials, self._settings.bedrock_region
            )
            self._token_expires_at = now + _TOKEN_REFRESH_S
        return self._token

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._bearer_token()}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages: list[dict], *, system: str | None = None) -> dict:
        payload: dict = {
            "messages": _converse_messages(messages),
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
        if system:
            payload["system"] = [{"text": system}]
        return payload

    def _finish(self, response: httpx.Response) -> dict:
        """Parse a completed response, recording its usage into the tally."""
        body = response.json()
        record_usage(*self._extract_usage(body))
        return body

    async def _run(self, messages: list[dict], *, system: str | None = None) -> dict:
        payload = self._payload(messages, system=system)
        try:
            response = await self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_text = ""
            try:
                body_text = exc.response.text[:300]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise UpstreamLLMError(
                f"Bedrock API request failed: {exc}: {body_text}".rstrip(": ")
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(f"Bedrock API request failed: {exc}") from exc
        return self._finish(response)

    async def generate_sql(
        self,
        question: str,
        history: list[Turn],
        validate: ValidateHook | None = None,
    ) -> SQLPlan:
        messages = _history_to_messages(history)
        messages.append({"role": "user", "content": question})

        body = await self._run(messages, system=STRICT_JSON_SQL_PROMPT)
        text = self._extract_text(body)
        tokens_in, tokens_out = self._extract_usage(body)
        parsed = parse_llm_json(text)
        sql = (parsed or {}).get("sql") or ""
        mode = (parsed or {}).get("mode") or ("db" if sql else "general")
        answer = (parsed or {}).get("answer") or None

        first_was_general = mode == "general"
        if mode == "db":
            error = validate(sql) if validate is not None else None
            if error is None:
                return SQLPlan(
                    sql=sql,
                    mode=mode,
                    answer=answer,
                    attempts=1,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            retry_reason = (
                f"Your previous SQL was invalid: {error}\n"
                "Fix it and respond again with the same STRICT JSON shape."
            )
        else:
            # Greetings/small-talk are already intercepted before the LLM is
            # called, so a non-DB ("general") reply to a real analytics question
            # is almost always a wrongful refusal (e.g. "I need to know your
            # zone"). Force a DB answer once.
            retry_reason = (
                "You MUST answer with a single MySQL SELECT query in the 'sql' "
                "field and mode='db'. Do NOT refuse, and do NOT ask who the user "
                "is or which zone/region/state they belong to — the user's scope "
                "is applied AUTOMATICALLY by the system after your SQL. NEVER use "
                "placeholder values like 'Your State', 'My Zone' or 'Your Zone'. "
                "Just compute the metric. Respond again with the STRICT JSON shape."
            )

        # Exactly one retry.
        retry_messages = list(messages)
        retry_messages.append({"role": "user", "content": retry_reason})
        retry_body = await self._run(retry_messages, system=STRICT_JSON_SQL_PROMPT)
        retry_text = self._extract_text(retry_body)
        r_in, r_out = self._extract_usage(retry_body)
        retry_parsed = parse_llm_json(retry_text)
        retry_sql = (retry_parsed or {}).get("sql") or ""
        retry_mode = (retry_parsed or {}).get("mode") or ("db" if retry_sql else "general")
        retry_answer = (retry_parsed or {}).get("answer") or None
        tot_in, tot_out = tokens_in + r_in, tokens_out + r_out

        if retry_mode == "db" and retry_sql.strip():
            retry_error = validate(retry_sql) if validate is not None else None
            if retry_error is None:
                return SQLPlan(
                    sql=retry_sql,
                    mode="db",
                    answer=retry_answer,
                    attempts=2,
                    tokens_in=tot_in,
                    tokens_out=tot_out,
                )
            # The retry produced invalid SQL. If the FIRST attempt was a real
            # data query, that's a genuine failure. But if the first attempt was
            # a general answer (e.g. "who is the PM of India?"), forcing SQL just
            # yields junk — fall through and return the conversational answer.
            if not first_was_general:
                raise UpstreamLLMError(
                    f"could not produce valid SQL after 2 attempts: {retry_error}"
                )

        # Retry came back general/empty, OR it was a general question we couldn't
        # (and shouldn't) turn into SQL. Return the conversational answer instead
        # of erroring — a general question is a valid outcome, not a failure.
        if first_was_general:
            return SQLPlan(
                sql="",
                mode="general",
                answer=retry_answer or answer,
                attempts=2,
                tokens_in=tot_in,
                tokens_out=tot_out,
            )
        raise UpstreamLLMError("could not produce valid SQL after 2 attempts")

    async def stream_answer(
        self,
        question: str,
        facts: dict,
        sample_rows: list[dict],
        columns: list[str],
    ) -> AsyncIterator[str]:
        prompt = ANSWER_PROMPT.format(
            question=question,
            facts=render_answer_facts(facts),
            columns=", ".join(columns),
            sample_rows=render_sample_rows(sample_rows[:5], columns),
        )
        messages = [{"role": "user", "content": prompt}]

        got_any = False
        try:
            async with self._client.stream(
                "POST",
                self._stream_url,
                headers=self._headers,
                json=self._payload(messages),
            ) as response:
                response.raise_for_status()
                buffer = EventStreamBuffer()
                async for chunk in response.aiter_bytes():
                    buffer.add_data(chunk)
                    for event in buffer:
                        if event.headers.get(":message-type") != "event":
                            continue
                        try:
                            payload = json.loads(event.payload.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        event_type = event.headers.get(":event-type")
                        if event_type == "metadata":
                            record_usage(*self._extract_usage(payload))
                            continue
                        if event_type != "contentBlockDelta":
                            continue
                        # Thinking arrives as delta.reasoningContent — internal
                        # chain-of-thought, never surfaced. Only text is.
                        text = (payload.get("delta") or {}).get("text")
                        if isinstance(text, str) and text:
                            got_any = True
                            yield text
        except httpx.HTTPError:
            # If ANY answer text was already yielded, never fall back — the
            # fallback would generate a second, different answer and the user
            # would see two answers in one message. A truncated single answer
            # beats a duplicated one.
            pass

        if got_any:
            return

        # Nothing streamed at all: fall back to a non-streaming call, yielded
        # as a single chunk.
        body = await self._run(messages)
        yield self._extract_text(body)

    async def rewrite_question(self, history: list[Turn], question: str) -> str:
        history_text = "\n".join(f"{turn.role}: {turn.text}" for turn in history)
        prompt = REWRITE_PROMPT.format(history=history_text, question=question)
        body = await self._run([{"role": "user", "content": prompt}])
        return self._extract_text(body).strip().strip('"')

    async def generate_title(self, text: str) -> str:
        prompt = TITLE_PROMPT.format(text=text)
        body = await self._run([{"role": "user", "content": prompt}])
        title = self._extract_text(body)
        return " ".join(title.strip().splitlines()[:1]).strip().strip('"')

    async def suggest_followups(
        self, question: str, columns: list[str], row_count: int
    ) -> list[str]:
        prompt = SUGGEST_FOLLOWUPS_PROMPT.format(
            question=question,
            columns=", ".join(columns) or "(none)",
            row_count=row_count,
        )
        body = await self._run([{"role": "user", "content": prompt}])
        return parse_suggestion_list(self._extract_text(body))

    @staticmethod
    def _extract_usage(body: dict) -> tuple[int, int]:
        """Return (input_tokens, output_tokens); (0, 0) if absent."""
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return 0, 0
        try:
            return int(usage.get("inputTokens") or 0), int(usage.get("outputTokens") or 0)
        except (TypeError, ValueError):
            return 0, 0

    @staticmethod
    def _extract_text(body: dict) -> str:
        """Pull the final message text out of a Converse response body.

        Joins the `text` items of `output.message.content` — `reasoningContent`
        blocks (Claude's thinking) are deliberately never surfaced.
        """
        message = (body.get("output") or {}).get("message") or {}
        parts = [
            item["text"]
            for item in message.get("content") or []
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)


__all__ = ["BedrockProvider"]
