"""Anthropic-backed `LLMProvider` implementation.

Uses the modern `anthropic` Python SDK (`AsyncAnthropic`), the Messages API,
a single `query_database` tool for structured SQL generation, and prompt
caching on the large static system block (see `app.llm.prompts`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import anthropic

from app.config import Settings
from app.errors import UpstreamLLMError
from app.llm.base import SQLPlan, Turn, ValidateHook, parse_suggestion_list
from app.llm.prompts import (
    ANSWER_PROMPT,
    REWRITE_PROMPT,
    SUGGEST_FOLLOWUPS_PROMPT,
    TITLE_PROMPT,
    build_dynamic_system_block,
    build_static_system_block,
    render_answer_facts,
    render_sample_rows,
)
from app.llm.usage import record_usage

_QUERY_DATABASE_TOOL = {
    "name": "query_database",
    "description": (
        "Submit a single read-only MySQL SELECT statement that answers the "
        "user's question, along with a brief explanation of your reasoning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single MySQL SELECT statement."},
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of how this SQL answers the question.",
            },
        },
        "required": ["sql"],
    },
}

_MAX_SQL_ATTEMPTS = 3


def _history_to_messages(history: list[Turn]) -> list[dict]:
    return [{"role": turn.role, "content": turn.text} for turn in history]


class AnthropicProvider:
    """`LLMProvider` implementation backed by the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._settings = settings
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate_sql(
        self,
        question: str,
        history: list[Turn],
        validate: ValidateHook | None = None,
    ) -> SQLPlan:
        static_block = build_static_system_block()
        dynamic_block = build_dynamic_system_block(
            today=datetime.now(timezone.utc).date(),
            role_hint="business user",
        )
        system = [
            {
                "type": "text",
                "text": static_block,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": dynamic_block},
        ]

        messages: list[dict] = _history_to_messages(history)
        messages.append({"role": "user", "content": question})

        tokens_in = 0
        tokens_out = 0
        cache_read_tokens = 0
        last_error: str | None = None

        for attempt in range(1, _MAX_SQL_ATTEMPTS + 1):
            try:
                response = await self._client.messages.create(
                    model=self._settings.anthropic_model_sql,
                    max_tokens=2000,
                    system=system,
                    tools=[_QUERY_DATABASE_TOOL],
                    tool_choice={"type": "auto"},
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise UpstreamLLMError(f"Anthropic API error while generating SQL: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - wrap any SDK-level failure (timeouts, etc.)
                raise UpstreamLLMError(
                    f"Anthropic request failed while generating SQL: {exc}"
                ) from exc

            usage = getattr(response, "usage", None)
            if usage is not None:
                attempt_in = getattr(usage, "input_tokens", 0) or 0
                attempt_out = getattr(usage, "output_tokens", 0) or 0
                tokens_in += attempt_in
                tokens_out += attempt_out
                cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
                record_usage(attempt_in, attempt_out)

            tool_use_block = None
            text_parts: list[str] = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "query_database":
                    tool_use_block = block
                elif getattr(block, "type", None) == "text":
                    text_parts.append(block.text)

            if tool_use_block is None:
                # Plain conversational reply — no SQL requested.
                return SQLPlan(
                    sql="",
                    mode="general",
                    answer="\n".join(text_parts).strip(),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_read_tokens=cache_read_tokens,
                    attempts=attempt,
                )

            sql = tool_use_block.input.get("sql", "")
            reasoning = tool_use_block.input.get("reasoning", "")

            error = validate(sql) if validate is not None else None
            if error is None:
                return SQLPlan(
                    sql=sql,
                    reasoning=reasoning,
                    mode="db",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_read_tokens=cache_read_tokens,
                    attempts=attempt,
                )

            last_error = error
            if attempt >= _MAX_SQL_ATTEMPTS:
                break

            # Feed the validation error back to the model as a tool error and retry.
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_block.id,
                            "content": error,
                            "is_error": True,
                        }
                    ],
                }
            )

        raise UpstreamLLMError(
            f"could not produce valid SQL after {_MAX_SQL_ATTEMPTS} attempts: {last_error}"
        )

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
        try:
            async with self._client.messages.stream(
                model=self._settings.anthropic_model_small,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                try:
                    final = await stream.get_final_message()
                    usage = getattr(final, "usage", None)
                    if usage is not None:
                        record_usage(
                            getattr(usage, "input_tokens", 0) or 0,
                            getattr(usage, "output_tokens", 0) or 0,
                        )
                except Exception:  # noqa: BLE001 - accounting must never break a delivered answer
                    pass
        except anthropic.APIError as exc:
            raise UpstreamLLMError(f"Anthropic API error while streaming answer: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise UpstreamLLMError(
                f"Anthropic request failed while streaming answer: {exc}"
            ) from exc

    async def rewrite_question(self, history: list[Turn], question: str) -> str:
        history_text = "\n".join(f"{turn.role}: {turn.text}" for turn in history)
        prompt = REWRITE_PROMPT.format(history=history_text, question=question)
        text = await self._small_completion(prompt, max_tokens=200)
        return text.strip().strip('"')

    async def generate_title(self, text: str) -> str:
        prompt = TITLE_PROMPT.format(text=text)
        title = await self._small_completion(prompt, max_tokens=30)
        return " ".join(title.strip().splitlines()[:1]).strip().strip('"')

    async def suggest_followups(
        self, question: str, columns: list[str], row_count: int
    ) -> list[str]:
        prompt = SUGGEST_FOLLOWUPS_PROMPT.format(
            question=question,
            columns=", ".join(columns) or "(none)",
            row_count=row_count,
        )
        text = await self._small_completion(prompt, max_tokens=200)
        return parse_suggestion_list(text)

    async def _small_completion(self, prompt: str, max_tokens: int) -> str:
        try:
            response = await self._client.messages.create(
                model=self._settings.anthropic_model_small,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise UpstreamLLMError(f"Anthropic API error: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise UpstreamLLMError(f"Anthropic request failed: {exc}") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            )
        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return "".join(parts)


__all__ = ["AnthropicProvider"]
