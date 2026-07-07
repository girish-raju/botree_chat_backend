"""Tests for `app/chat/answerer.py` — markdown table + answer scrubbing."""

from __future__ import annotations

from decimal import Decimal

from app.chat.answerer import (
    render_markdown_table,
    scrub_raw_row_dumps,
    stream_answer_text,
)


class _EchoProvider:
    """Provider stub whose stream_answer yields the given chunks."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_answer(self, question, facts, sample_rows, columns):
        for chunk in self._chunks:
            yield chunk


async def _answer_text(chunks: list[str]) -> str:
    facts = {"sample": [], "lead_in": "I found 2 matching rows."}
    parts = [
        d
        async for d in stream_answer_text(
            _EchoProvider(chunks), "q", facts, ["a"], [{"a": 1}, {"a": 2}]
        )
    ]
    return "".join(parts)


def test_scrub_removes_echoed_row_dump() -> None:
    text = (
        "The top brand is BHUJIA.\n\n"
        "[{'product_name': 'BHUJIA 32 GM.', 'State': 'WB STATE', 'TotalSales': "
        "20331947.478052}, {'product_name': 'BHUJIA 200 GM.', 'State': 'WB "
        "STATE', 'TotalSales': 18150012.810142}]\n\nSales are strong."
    )
    assert scrub_raw_row_dumps(text) == "The top brand is BHUJIA.\n\nSales are strong."


def test_scrub_removes_unterminated_dump_tail() -> None:
    text = "Total is high. [{'product_name': 'BHUJIA 32 GM.', 'TotalSales'"
    assert scrub_raw_row_dumps(text) == "Total is high."


def test_scrub_keeps_clean_text_untouched() -> None:
    text = "Total sales across all geographies is ₹10,21,48,341.79."
    assert scrub_raw_row_dumps(text) == text


def test_scrub_removes_llm_written_markdown_table() -> None:
    text = (
        "The total sales across all months is 1,079,287.11 rupees.\n\n"
        "| invoice_date | TotalSales |\n"
        "| --- | --- |\n"
        "| 2024-03-03 | 847626.90027 |\n"
        "| 2024-03-11 | 64096.435017 |\n\n"
        "March 2024 has the highest sales."
    )
    assert scrub_raw_row_dumps(text) == (
        "The total sales across all months is 1,079,287.11 rupees.\n\n"
        "March 2024 has the highest sales."
    )


async def test_stream_answer_scrubs_dump_from_llm_output() -> None:
    text = await _answer_text(
        ["Top brand is BHUJIA. ", "[{'a': 1}, ", "{'a': 2}]", " Great month."]
    )
    assert "[{" not in text
    assert "Top brand is BHUJIA." in text
    assert "Great month." in text


async def test_stream_answer_falls_back_to_lead_in_when_all_noise() -> None:
    text = await _answer_text(["[{'a': 1}]"])
    assert text == "I found 2 matching rows."


def test_renders_all_rows_not_a_sample() -> None:
    columns = ["product_name", "TotalSales"]
    rows = [{"product_name": f"P{i}", "TotalSales": float(i)} for i in range(50)]

    table = render_markdown_table(columns, rows)

    lines = table.splitlines()
    assert lines[0] == "| product_name | TotalSales |"
    assert lines[1] == "| --- | --- |"
    assert len(lines) == 2 + 50  # header + separator + every row
    assert "| P49 |" in lines[-1]


def test_money_columns_formatted_as_rupees() -> None:
    table = render_markdown_table(
        ["product_name", "TotalSales"],
        [{"product_name": "ALL MIX 35 gm.", "TotalSales": Decimal("2473225.875555")}],
    )

    assert "₹24,73,225.88" in table
    assert "2473225.875555" not in table


def test_scalar_result_gets_no_table() -> None:
    # 1 row x 1 column ("what's the total sales this month") stays prose-only.
    assert render_markdown_table(["total"], [{"total": 42.0}]) == ""


def test_empty_result_gets_no_table() -> None:
    assert render_markdown_table(["a"], []) == ""
    assert render_markdown_table([], [{"a": 1}]) == ""


def test_single_row_multi_column_still_gets_table() -> None:
    table = render_markdown_table(
        ["state", "total"], [{"state": "KARNATAKA", "total": 10.0}]
    )
    assert "| KARNATAKA |" in table


def test_cells_escape_pipes_and_newlines_and_none() -> None:
    table = render_markdown_table(
        ["name|note", "value"],
        [{"name|note": "a|b\nc", "value": None}],
    )
    lines = table.splitlines()
    assert lines[0] == "| name\\|note | value |"
    assert lines[2] == "| a\\|b c |  |"
