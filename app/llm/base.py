"""Shared types and the `LLMProvider` protocol for the NL->SQL / NL-answer backends.

Concrete providers (Anthropic, Cloudflare) live in sibling modules and are
selected at runtime via `app.llm.factory.get_provider`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass
class SQLPlan:
    """Result of asking a provider to turn a question into SQL (or a direct answer)."""

    sql: str
    reasoning: str = ""
    mode: Literal["db", "general"] = "db"
    answer: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    attempts: int = 1


@dataclass
class Turn:
    """A single message in the conversation history passed to the LLM."""

    role: Literal["user", "assistant"]
    text: str


#: Injected by the pipeline (SQL-safety / RBAC layer). Given a candidate SQL
#: string, returns `None` if it's acceptable, or a human-readable error
#: message the model should self-correct against.
ValidateHook = Callable[[str], "str | None"]


@runtime_checkable
class LLMProvider(Protocol):
    """Interface every LLM backend (Anthropic, Cloudflare, ...) must implement."""

    name: str

    async def generate_sql(
        self,
        question: str,
        history: list[Turn],
        validate: ValidateHook | None = None,
    ) -> SQLPlan:
        """Turn `question` (+ `history`) into a `SQLPlan`.

        If `validate` is provided, the returned SQL must have passed it (the
        provider retries against the model using the validation error as
        feedback, up to its own internal attempt limit).
        """
        ...

    async def stream_answer(
        self,
        question: str,
        facts: dict,
        sample_rows: list[dict],
        columns: list[str],
    ) -> AsyncIterator[str]:
        """Yield text deltas composing a natural-language answer grounded in `facts`."""
        ...
        yield ""  # pragma: no cover - protocol body, never executed

    async def rewrite_question(self, history: list[Turn], question: str) -> str:
        """Resolve pronouns/ellipsis in `question` into a standalone question."""
        ...

    async def generate_title(self, text: str) -> str:
        """Generate a short (<=6 word) title summarizing `text`."""
        ...


__all__ = ["SQLPlan", "Turn", "ValidateHook", "LLMProvider"]
