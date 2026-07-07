"""Tests for `app/chat/answerer.py` — the deterministic markdown result table."""

from __future__ import annotations

from decimal import Decimal

from app.chat.answerer import render_markdown_table


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
