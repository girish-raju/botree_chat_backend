"""Deterministic fact computation + natural-language answer streaming.

`build_facts` is pure and deterministic: it sums numeric columns via
`compute_total_facts` (the anti-hallucination guard), formats money totals as
rupees, and attaches a small sample of rows plus a fast deterministic lead-in.
`stream_answer_text` streams the prose answer from the LLM — except for empty
result sets, which get a deterministic "no matching data" sentence with no LLM
call at all. `render_markdown_table` renders the FULL result set as a markdown
table, appended to every data-driven answer by the pipeline so the user always
sees all rows (the LLM only writes the short summary above it).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from app.cache.results import jsonable_rows
from app.domain.formatting import (
    compute_total_facts,
    format_rupees,
    is_money_column,
    regroup_western_numbers,
)
from app.llm.base import LLMProvider

#: Deterministic answer for an empty result set (no LLM call).
NO_DATA_SENTENCE = "I couldn't find any matching data for that question."

_MAX_SAMPLE_ROWS = 5

#: A raw row dump echoed by the LLM: "[{...}]" possibly spanning lines.
_ROW_DUMP_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)

#: Column-aligned text (3+ consecutive inner spaces), e.g. "WB STATE   44000.0".
#: Requires 3+ so incidental double spaces in prose (e.g. left behind by dump
#: removal) never get a sentence dropped; 2-space-aligned data rows are still
#: caught by the trailing-number rule below.
_ALIGNED_COLUMNS_RE = re.compile(r"\S\s{3,}\S")

#: A line whose last token is a bare/rupee number, e.g. "KERALA STATE 38000.0".
_TRAILING_NUMBER_RE = re.compile(r"₹?[\d,]+(?:\.\d+)?$")


def _is_data_line(line: str) -> bool:
    """True for lines that are table/row echoes rather than prose sentences."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("|"):
        return True  # markdown table row
    if _ALIGNED_COLUMNS_RE.search(stripped):
        return True  # whitespace-aligned columns
    # Short label+number lines ("TAMILNADU STATE 121000.0") are row echoes;
    # real sentences are longer and end with punctuation, not a raw number.
    return bool(_TRAILING_NUMBER_RE.search(stripped)) and len(stripped.split()) <= 5


def scrub_raw_row_dumps(text: str) -> str:
    """Remove echoed data from LLM answer text, keeping only the prose.

    Small models sometimes parrot the grounding rows from the prompt back
    into their answer — as a Python/JSON list of dicts (`[{'col': ...}]`),
    a self-made markdown table, or whitespace-aligned columns. The real,
    complete table is ALWAYS appended deterministically by the pipeline, so
    any data structure in the LLM's own text is a duplicate: strip complete
    `[{...}]` spans, any unterminated `[{...` tail, every table-like line
    (see `_is_data_line`), and tidy the leftover whitespace.
    """
    cleaned = _ROW_DUMP_RE.sub("", text)
    start = cleaned.find("[{")
    if start != -1 and "}]" not in cleaned[start:]:
        cleaned = cleaned[:start]
    cleaned = "\n".join(
        line for line in cleaned.splitlines() if not _is_data_line(line)
    )
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def render_markdown_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Render the full result set as a GitHub-flavored markdown table.

    Deterministic — the LLM never sees or produces this, so every row the
    query returned is shown, with money columns formatted as rupees. Returns
    "" when a table adds nothing: no rows, no columns, or a single scalar
    (1 row x 1 column), whose value the prose answer already states.
    """
    if not rows or not columns or (len(rows) == 1 and len(columns) == 1):
        return ""

    def escape(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    def cell(col: str, value: Any) -> str:
        if value is None:
            return ""
        if is_money_column(col) and isinstance(value, (int, float)) and not isinstance(value, bool):
            return format_rupees(value)
        return escape(str(value))

    lines = [
        "| " + " | ".join(escape(col) for col in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in jsonable_rows(rows):
        lines.append("| " + " | ".join(cell(col, row.get(col)) for col in columns) + " |")
    return "\n".join(lines)


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
    """Yield the natural-language answer text.

    For empty result sets, yields a single deterministic sentence WITHOUT
    calling the LLM. Otherwise delegates to `provider.stream_answer`, grounded
    in the deterministic `facts` and a small sample of rows. The LLM output is
    buffered and scrubbed of echoed raw-row dumps before being yielded — a
    guaranteed-clean short summary beats token-streaming noise the frontend
    would have to unrender. Falls back to the deterministic lead-in if
    scrubbing leaves nothing.
    """
    if not rows:
        yield NO_DATA_SENTENCE
        return

    sample_rows = facts.get("sample", [])
    chunks: list[str] = []
    async for delta in provider.stream_answer(question, facts, sample_rows, columns):
        if delta:
            chunks.append(delta)

    answer = regroup_western_numbers(scrub_raw_row_dumps("".join(chunks)))
    yield answer or str(facts.get("lead_in", ""))


__all__ = [
    "build_facts",
    "stream_answer_text",
    "render_markdown_table",
    "scrub_raw_row_dumps",
    "NO_DATA_SENTENCE",
]
