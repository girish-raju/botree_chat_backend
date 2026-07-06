"""Deterministic fact computation + natural-language answer streaming.

`build_facts` is pure and deterministic: it sums numeric columns via
`compute_total_facts` (the anti-hallucination guard), formats money totals as
rupees, and attaches a small sample of rows plus a fast deterministic lead-in.
`stream_answer_text` streams the prose answer from the LLM — except for empty
result sets, which get a deterministic "no matching data" sentence with no LLM
call at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.cache.results import jsonable_rows
from app.domain.formatting import compute_total_facts, format_rupees, is_money_column
from app.llm.base import LLMProvider

#: Deterministic answer for an empty result set (no LLM call).
NO_DATA_SENTENCE = "I couldn't find any matching data for that question."

_MAX_SAMPLE_ROWS = 5


def _lead_in(row_count: int) -> str:
    if row_count == 0:
        return NO_DATA_SENTENCE
    if row_count == 1:
        return "Here's what I found:"
    return f"I found {row_count} matching rows."


async def build_facts(
    question: str, columns: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute deterministic facts + a small sample for grounding the answer.

    `rows` are dict rows (as returned by the analytics layer / result cache).
    Returns a dict with the raw totals, rupee-formatted totals for money
    columns, the target columns summed, a truncation flag, a `<=5` row sample,
    and a deterministic lead-in string.
    """
    positional = [[row.get(col) for col in columns] for row in rows]
    facts = compute_total_facts(columns, positional)

    totals = facts.get("totals", {})
    totals_display: dict[str, Any] = {}
    for col, total in totals.items():
        totals_display[col] = format_rupees(total) if is_money_column(col) else total

    sample = jsonable_rows(rows)[:_MAX_SAMPLE_ROWS]

    return {
        "question": question,
        "row_count": len(rows),
        "truncated": facts.get("truncated", False),
        "target_columns": facts.get("target_columns", []),
        "totals": totals,
        "totals_display": totals_display,
        "sample": sample,
        "lead_in": _lead_in(len(rows)),
    }


async def stream_answer_text(
    provider: LLMProvider,
    question: str,
    facts: dict[str, Any],
    columns: list[str],
    rows: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """Yield the natural-language answer as text deltas.

    For empty result sets, yields a single deterministic sentence WITHOUT
    calling the LLM. Otherwise delegates to `provider.stream_answer`, grounded
    in the deterministic `facts` and a small sample of rows.
    """
    if not rows:
        yield NO_DATA_SENTENCE
        return

    sample_rows = facts.get("sample", [])
    async for delta in provider.stream_answer(question, facts, sample_rows, columns):
        if delta:
            yield delta


__all__ = ["build_facts", "stream_answer_text", "NO_DATA_SENTENCE"]
