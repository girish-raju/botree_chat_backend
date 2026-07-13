"""Cloudflare Workers AI-backed `LLMProvider` implementation.

Talks to `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}`
over plain HTTP via `httpx`. Unlike the Anthropic provider (which uses a tool
call), this provider asks the model for strict JSON and repairs the response
text if the model doesn't quite manage strict JSON — the repair steps are a
direct port of `parse_llm_json` from the prototype
(`conversational_bot_v15.py`).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.errors import UpstreamLLMError
from app.llm.base import SQLPlan, Turn, ValidateHook, parse_suggestion_list
from app.llm.prompts import (
    ANSWER_PROMPT,
    CLOUDFLARE_SQL_PROMPT,
    REWRITE_PROMPT,
    SUGGEST_FOLLOWUPS_PROMPT,
    TITLE_PROMPT,
    render_answer_facts,
    render_sample_rows,
)

_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def _try_parse(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _sanitize(text: str) -> str:
    """Fix the most common LLM JSON breakages (ported from `parse_llm_json`).

    1. Strip markdown code fences.
    2. Collapse escaped newlines inside string values into a space.
    3. Un-escape escaped single quotes that break JSON.
    """
    text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    text = re.sub(r"(?<!\\)\\n", " ", text)
    text = text.replace("\\'", "'")
    return text


def parse_llm_json(content: str) -> dict | None:
    """Parse a (possibly malformed) JSON object out of raw LLM text output.

    Ported from `parse_llm_json` in `conversational_bot_v15.py`: tries a
    direct parse, then sanitized parse, then extracts the outermost
    `{...}` span and sanitizes that, then a regex-based first-object
    extraction, then falls back to trailing-comma repair and a
    single-quote-to-double-quote conversion. Returns `None` if nothing works.
    """
    if not content or not content.strip():
        return None

    # Step 1: direct parse.
    result = _try_parse(content)
    if result is not None:
        return result

    # Step 2: sanitize then parse.
    sanitized = _sanitize(content)
    result = _try_parse(sanitized)
    if result is not None:
        return result

    # Step 3: extract outermost { } then sanitize.
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        extracted = _sanitize(content[start:end])
        result = _try_parse(extracted)
        if result is not None:
            return result
    except ValueError:
        pass

    # Step 4: regex find the first complete-looking JSON object (greedy across the string).
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        candidate = _sanitize(match.group())
        result = _try_parse(candidate)
        if result is not None:
            return result

        # Step 5: strip trailing commas before `}` / `]`.
        no_trailing_commas = re.sub(r",\s*([}\]])", r"\1", candidate)
        result = _try_parse(no_trailing_commas)
        if result is not None:
            return result

        # Step 6: last resort — convert single-quoted keys/values to double quotes.
        single_to_double = re.sub(r"'", '"', no_trailing_commas)
        result = _try_parse(single_to_double)
        if result is not None:
            return result

    return None


def _history_to_messages(history: list[Turn]) -> list[dict]:
    return [{"role": turn.role, "content": turn.text} for turn in history]


class CloudflareProvider:
    """`LLMProvider` implementation backed by Cloudflare Workers AI."""

    name = "cloudflare"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._own_client = client is None

    @property
    def _url(self) -> str:
        return _BASE_URL.format(
            account_id=self._settings.cloudflare_account_id,
            model=self._settings.cloudflare_model,
        )

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._settings.cloudflare_api_token}"}

    async def _run(self, payload: dict) -> dict:
        try:
            response = await self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(f"Cloudflare API request failed: {exc}") from exc
        return response.json()

    async def generate_sql(
        self,
        question: str,
        history: list[Turn],
        validate: ValidateHook | None = None,
    ) -> SQLPlan:
        messages = [{"role": "system", "content": CLOUDFLARE_SQL_PROMPT}]
        messages.extend(_history_to_messages(history))
        messages.append({"role": "user", "content": question})

        body = await self._run({"messages": messages})
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
                    sql=sql, mode=mode, answer=answer, attempts=1,
                    tokens_in=tokens_in, tokens_out=tokens_out,
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
        retry_body = await self._run({"messages": retry_messages})
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
                    sql=retry_sql, mode="db", answer=retry_answer, attempts=2,
                    tokens_in=tot_in, tokens_out=tot_out,
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
                sql="", mode="general", answer=retry_answer or answer, attempts=2,
                tokens_in=tot_in, tokens_out=tot_out,
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
        payload = {"messages": [{"role": "user", "content": prompt}], "stream": True}

        got_any = False
        try:
            async with self._client.stream(
                "POST", self._url, headers=self._headers, json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    delta = chunk.get("response", "")
                    if delta != "" and delta is not None:
                        got_any = True
                        yield delta if isinstance(delta, str) else str(delta)
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
        body = await self._run({"messages": [{"role": "user", "content": prompt}]})
        yield self._extract_text(body)

    async def rewrite_question(self, history: list[Turn], question: str) -> str:
        history_text = "\n".join(f"{turn.role}: {turn.text}" for turn in history)
        prompt = REWRITE_PROMPT.format(history=history_text, question=question)
        body = await self._run({"messages": [{"role": "user", "content": prompt}]})
        return self._extract_text(body).strip().strip('"')

    async def generate_title(self, text: str) -> str:
        prompt = TITLE_PROMPT.format(text=text)
        body = await self._run({"messages": [{"role": "user", "content": prompt}]})
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
        body = await self._run({"messages": [{"role": "user", "content": prompt}]})
        return parse_suggestion_list(self._extract_text(body))

    @staticmethod
    def _extract_usage(body: dict) -> tuple[int, int]:
        """Return (prompt_tokens, completion_tokens) from a Workers AI response.

        Usage lives at ``result.usage`` in the OpenAI-compatible shape
        (``prompt_tokens`` / ``completion_tokens``); return (0, 0) if absent.
        """
        result = body.get("result", body)
        usage = result.get("usage") if isinstance(result, dict) else None
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
        """Pull the model's text out of a Workers AI response.

        The envelope varies by model/version: `result.response` may be a plain
        string, or a chat-style dict like ``{"content": "...", "tool_calls": []}``
        (Llama 3.1 with the OpenAI-compatible response shape). Handle both, and
        fall back to a tool-call's arguments if that's where the JSON landed.
        """
        result = body.get("result", body)
        if not isinstance(result, dict):
            return str(result or "")

        response = result.get("response")
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            # vLLM/Workers AI with a JSON response format already parses the
            # model output into a dict, e.g. {"mode":"db","sql":"...","answer":""}.
            if any(k in response for k in ("sql", "mode", "answer")):
                return json.dumps(response)
            # Chat-style shape: {"content": "...", "tool_calls": [...]}.
            content = response.get("content")
            if isinstance(content, str) and content.strip():
                return content
            for call in response.get("tool_calls") or []:
                args = (call or {}).get("function", {}).get("arguments") or call.get("arguments")
                if isinstance(args, str) and args.strip():
                    return args
                if isinstance(args, dict):
                    return json.dumps(args)
            # Unknown dict shape — hand the whole thing to the JSON parser.
            return json.dumps(response)

        # OpenAI-compatible fallback: result.choices[0].message.content.
        for choice in result.get("choices") or []:
            msg = (choice or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content

        # Older/simple shape: text under other top-level keys.
        for key in ("output", "text", "content"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""


__all__ = ["CloudflareProvider", "parse_llm_json"]
