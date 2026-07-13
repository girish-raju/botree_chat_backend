"""Amazon Bedrock-backed `LLMProvider` implementation (OpenAI-compatible API).

Talks to Bedrock's OpenAI-compatible chat-completions endpoint
(`https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions`)
over plain HTTP via `httpx`, authenticated with a bearer token: either a
Bedrock API key from settings, or one auto-generated from plain IAM keys via
`aws-bedrock-token-generator` (pure SigV4 presigning, no network call).

Serves reasoning models like gpt-oss-20b; reasoning is pinned low (both the
`reasoning_effort` param and a `Reasoning: low` system hint). Bedrock's
gpt-oss serving emits chain-of-thought INLINE in `content` as
`<reasoning>...</reasoning>` blocks (observed live on ap-south-1) — these are
stripped from every response, including tags split across stream chunks, so
only the final answer ever reaches the user.

Control flow (strict-JSON prompt, `parse_llm_json` repair, exactly one forced
retry, streaming with non-streaming fallback) mirrors `CloudflareProvider`.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator

import httpx
from aws_bedrock_token_generator import BedrockTokenGenerator
from botocore.credentials import Credentials

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

_CHAT_URL = "https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions"

# gpt-oss reads a "Reasoning: low|medium|high" directive from the system
# prompt; kept alongside the reasoning_effort param so latency stays low even
# if the endpoint drops the param.
_REASONING_HINT = "Reasoning: low"

# Reasoning tokens count against the completion budget, so leave headroom
# beyond what the strict-JSON plan itself needs.
_MAX_COMPLETION_TOKENS = 2048

# Generated bearer tokens are valid 12h; refresh well before that.
_TOKEN_REFRESH_S = 6 * 3600

_REASONING_OPEN = "<reasoning>"
_REASONING_CLOSE = "</reasoning>"
_REASONING_BLOCK_RE = re.compile(r"<reasoning>.*?(?:</reasoning>|\Z)", re.DOTALL)


def _strip_reasoning(text: str) -> str:
    """Drop inline `<reasoning>...</reasoning>` chain-of-thought blocks."""
    return _REASONING_BLOCK_RE.sub("", text)


def _partial_tag_len(text: str, tag: str) -> int:
    """Length of the longest strict prefix of `tag` that `text` ends with."""
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


class _ReasoningStreamFilter:
    """Incrementally removes `<reasoning>...</reasoning>` spans from a stream.

    Tags can be split across chunk boundaries, so a potential partial tag at
    the end of the buffer is held back until the next chunk resolves it.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._inside:
                end = self._buf.find(_REASONING_CLOSE)
                if end == -1:
                    # Still inside reasoning: discard all but a possible
                    # partial closing tag at the tail.
                    self._buf = self._buf[
                        len(self._buf) - _partial_tag_len(self._buf, _REASONING_CLOSE) :
                    ]
                    break
                self._buf = self._buf[end + len(_REASONING_CLOSE) :]
                self._inside = False
            else:
                start = self._buf.find(_REASONING_OPEN)
                if start == -1:
                    # Emit everything except a possible partial opening tag.
                    safe = len(self._buf) - _partial_tag_len(self._buf, _REASONING_OPEN)
                    out.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(_REASONING_OPEN) :]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        """Emit whatever is held back (a partial tag that never completed)."""
        remainder = "" if self._inside else self._buf
        self._buf = ""
        return remainder


def _history_to_messages(history: list[Turn]) -> list[dict]:
    return [{"role": turn.role, "content": turn.text} for turn in history]


class BedrockProvider:
    """`LLMProvider` implementation backed by Amazon Bedrock (gpt-oss et al.)."""

    name = "bedrock"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        # Reasoning models have higher first-token latency than plain chat
        # models — allow more than the Cloudflare provider's 30s.
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._own_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def _url(self) -> str:
        return _CHAT_URL.format(region=self._settings.bedrock_region)

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

    def _payload(self, messages: list[dict], *, stream: bool = False) -> dict:
        payload = {
            "model": self._settings.bedrock_model,
            "messages": messages,
            "max_completion_tokens": _MAX_COMPLETION_TOKENS,
            "temperature": 0.1,
            "reasoning_effort": "low",
        }
        if stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def _system_prompt(base: str) -> str:
        return f"{_REASONING_HINT}\n\n{base}"

    async def _run(self, messages: list[dict]) -> dict:
        payload = self._payload(messages)
        try:
            response = await self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_text = ""
            try:
                body_text = exc.response.text[:300]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            # Least-certain endpoint detail: reasoning_effort acceptance. If
            # the API rejects the param, drop it and try once more.
            if exc.response.status_code == 400 and "reasoning_effort" in body_text:
                retry_payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
                try:
                    response = await self._client.post(
                        self._url, headers=self._headers, json=retry_payload
                    )
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPError as retry_exc:
                    raise UpstreamLLMError(
                        f"Bedrock API request failed: {retry_exc}"
                    ) from retry_exc
            raise UpstreamLLMError(
                f"Bedrock API request failed: {exc}: {body_text}".rstrip(": ")
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(f"Bedrock API request failed: {exc}") from exc
        return response.json()

    async def generate_sql(
        self,
        question: str,
        history: list[Turn],
        validate: ValidateHook | None = None,
    ) -> SQLPlan:
        messages = [{"role": "system", "content": self._system_prompt(STRICT_JSON_SQL_PROMPT)}]
        messages.extend(_history_to_messages(history))
        messages.append({"role": "user", "content": question})

        body = await self._run(messages)
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
        retry_body = await self._run(retry_messages)
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
        reasoning_filter = _ReasoningStreamFilter()
        try:
            async with self._client.stream(
                "POST",
                self._url,
                headers=self._headers,
                json=self._payload(messages, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # The final chunk may carry usage only, with empty choices.
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0] or {}).get("delta") or {}
                    # Reasoning deltas (delta.reasoning_content) are internal
                    # chain-of-thought — never surface them.
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        # gpt-oss on Bedrock inlines chain-of-thought in the
                        # content stream as <reasoning>...</reasoning>.
                        visible = reasoning_filter.feed(content)
                        if visible:
                            got_any = True
                            yield visible
                tail = reasoning_filter.flush()
                if tail:
                    got_any = True
                    yield tail
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
        """Return (prompt_tokens, completion_tokens); (0, 0) if absent."""
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return 0, 0
        tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        tokens_out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        try:
            return int(tokens_in), int(tokens_out)
        except (TypeError, ValueError):
            return 0, 0

    @staticmethod
    def _extract_text(body: dict) -> str:
        """Pull the final message text out of an OpenAI chat-completions body.

        Reads only `message.content` — `message.reasoning_content` (gpt-oss
        chain-of-thought on some servings) is deliberately never surfaced, and
        inline `<reasoning>...</reasoning>` blocks (the shape Bedrock's
        gpt-oss serving actually emits) are stripped. `content` may also be a
        list of typed parts on some OpenAI-compatible servers; join the text
        parts in that case.
        """
        for choice in body.get("choices") or []:
            msg = (choice or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return _strip_reasoning(content)
            if isinstance(content, list):
                parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                joined = "".join(parts)
                if joined.strip():
                    return _strip_reasoning(joined)
        return ""


__all__ = ["BedrockProvider"]
