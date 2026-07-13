"""Shared types and the `LLMProvider` protocol for the NL->SQL / NL-answer backends.

Concrete providers (Anthropic, Cloudflare) live in sibling modules and are
selected at runtime via `app.llm.factory.get_provider`.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

#: Hard cap on follow-up suggestions shown to the user.
MAX_SUGGESTIONS = 3

#: Max characters per suggestion (anything longer is a hallucinated essay).
_MAX_SUGGESTION_LEN = 120

_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s*)")


def parse_suggestion_list(text: str) -> list[str]:
    """Parse an LLM reply into at most MAX_SUGGESTIONS follow-up strings.

    Tolerant by design — models wrap the JSON array in prose or fences, or
    return junk entirely. Tries the raw text, then the first (non-greedy)
    bracketed span, then the widest one. Non-string entries, blanks, and
    case-insensitive duplicates are dropped; leading list markers stripped.
    Returns [] on ANY failure: no suggestions is always a safe outcome.
    """
    text = (text or "").strip()
    candidates = [text]
    for pattern in (r"\[.*?\]", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidates.append(match.group(0))
    raw: list | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            raw = parsed
            break
    if raw is None:
        return []

    seen: set[str] = set()
    items: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = _LIST_MARKER_RE.sub("", entry).strip().strip('"').strip()
        if not cleaned or len(cleaned) > _MAX_SUGGESTION_LEN:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
        if len(items) == MAX_SUGGESTIONS:
            break
    return items


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

    async def suggest_followups(
        self, question: str, columns: list[str], row_count: int
    ) -> list[str]:
        """Suggest 0..MAX_SUGGESTIONS short follow-up questions for `question`.

        Best-effort: callers treat any error as "no suggestions"."""
        ...


__all__ = [
    "SQLPlan",
    "Turn",
    "ValidateHook",
    "LLMProvider",
    "MAX_SUGGESTIONS",
    "parse_suggestion_list",
]
