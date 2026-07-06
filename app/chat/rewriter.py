"""Follow-up question rewriting (anaphora / ellipsis resolution).

A user's second question in a thread is often a fragment that only makes sense
against the previous turn ("break that down by region", "what about last
month"). `maybe_rewrite` cheaply decides whether a question needs the LLM's
`rewrite_question` to be turned into a standalone question, and never raises:
any LLM failure falls back to the original question.
"""

from __future__ import annotations

import re

import structlog

from app.llm.base import LLMProvider, Turn

logger = structlog.get_logger(__name__)

#: Leading phrases that mark a question as a follow-up needing context.
_FOLLOWUP_PREFIXES: tuple[str, ...] = (
    "what about",
    "how about",
    "what if",
    "break that",
    "break it",
    "break down",
    "drill down",
    "drill into",
    "same for",
    "same but",
    "same thing",
    "and what",
    "and how",
    "now show",
    "now give",
)

#: First-token words that signal an anaphoric reference to a prior turn.
_ANAPHORA_WORDS: frozenset[str] = frozenset(
    {
        "it",
        "its",
        "that",
        "this",
        "these",
        "those",
        "them",
        "they",
        "their",
        "he",
        "she",
        "his",
        "her",
        "and",
        "also",
        "then",
    }
)

_WORD_RE = re.compile(r"[a-z]+")


def _looks_standalone(question: str) -> bool:
    """Cheap heuristic: True if `question` needs no prior-turn context."""
    q = (question or "").strip().lower()
    if not q:
        return True
    for phrase in _FOLLOWUP_PREFIXES:
        if q.startswith(phrase):
            return False
    words = _WORD_RE.findall(q)
    if words and words[0] in _ANAPHORA_WORDS:
        return False
    return True


async def maybe_rewrite(
    provider: LLMProvider, history: list[Turn], question: str
) -> tuple[str, bool]:
    """Return `(question_to_use, was_rewritten)`.

    Returns `(question, False)` unchanged when there is no history or the
    question already looks standalone. Otherwise delegates to
    `provider.rewrite_question`; on any error (or an empty result) it falls
    back to `(question, False)`. Never raises.
    """
    if not history or _looks_standalone(question):
        return question, False

    try:
        rewritten = await provider.rewrite_question(history, question)
    except Exception:
        logger.warning("rewrite_failed", exc_info=True)
        return question, False

    rewritten = (rewritten or "").strip()
    if not rewritten:
        return question, False
    return rewritten, True


__all__ = ["maybe_rewrite"]
