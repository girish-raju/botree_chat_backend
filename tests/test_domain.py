"""Tests for the app.domain semantic-layer package (Phase 4a)."""

from __future__ import annotations

from app.domain.formatting import (
    GREETING_REPLY,
    compute_total_facts,
    format_rupees,
    is_greeting,
    is_money_column,
    is_yoy_mtd_comparison,
)
from app.domain.glossary import BUSINESS_GLOSSARY, STATE_NAME_MAP, SYNONYMS
from app.domain.schema_catalog import (
    FACT_TABLES,
    INCLUDE_TABLES,
    SCHEMA_DESCRIPTION,
    format_schema_description,
)
from app.domain.sql_rules import FEW_SHOT_EXAMPLES, SQL_RULES


# ============================================================
# schema_catalog
# ============================================================


def test_include_tables_has_ten_entries():
    assert len(INCLUDE_TABLES) == 10


def test_all_include_tables_present_in_schema_description():
    for table in INCLUDE_TABLES:
        assert table in SCHEMA_DESCRIPTION, f"{table} missing from SCHEMA_DESCRIPTION"


def test_fact_tables_are_subset_of_include_tables():
    assert FACT_TABLES <= INCLUDE_TABLES
    assert len(FACT_TABLES) == 5


def test_format_schema_description_mentions_every_table():
    text = format_schema_description()
    for table in INCLUDE_TABLES:
        assert table in text


def test_schema_description_columns_are_nonempty_strings():
    for table, cols in SCHEMA_DESCRIPTION.items():
        assert cols, f"{table} has no columns"
        for col, desc in cols.items():
            assert isinstance(desc, str) and desc.strip()


# ============================================================
# sql_rules
# ============================================================


def test_sql_rules_mentions_key_business_facts():
    assert "measure_14" in SQL_RULES
    assert "CURDATE" in SQL_RULES
    assert "NEVER JOIN TWO FACT TABLES" in SQL_RULES.upper() or "NEVER JOIN TWO FACT TABLES" in SQL_RULES


def test_few_shot_examples_nonempty_and_well_formed():
    assert len(FEW_SHOT_EXAMPLES) > 0
    for example in FEW_SHOT_EXAMPLES:
        assert "question" in example and example["question"].strip()
        assert "sql" in example and example["sql"].strip()


def test_few_shot_examples_reference_only_whitelisted_tables():
    for example in FEW_SHOT_EXAMPLES:
        sql = example["sql"]
        referenced = {table for table in INCLUDE_TABLES if table in sql}
        assert referenced, f"No whitelisted table referenced in: {sql}"


# ============================================================
# glossary
# ============================================================


def test_business_glossary_has_core_role_abbreviations():
    for term in ("ASM", "RSM", "ZSM", "BM", "SO", "VP"):
        assert term in BUSINESS_GLOSSARY


def test_state_name_map_has_expected_entries():
    assert STATE_NAME_MAP["Tamil Nadu"] == "TAMILNADU STATE"
    assert STATE_NAME_MAP["West Bengal"] == "WB STATE"


def test_synonyms_nonempty():
    assert SYNONYMS
    assert SYNONYMS["sales"] == "revenue"


# ============================================================
# formatting — greetings
# ============================================================


def test_is_greeting_true_cases():
    assert is_greeting("hi")
    assert is_greeting("hello")
    assert is_greeting("good morning")
    assert is_greeting("Hi!")
    assert is_greeting("thanks")


def test_is_greeting_false_cases():
    assert not is_greeting("what are sales today")
    assert not is_greeting("what is my ytd sales by state and zone")


def test_is_greeting_rejects_long_prompts_even_with_keyword():
    # word_count > 4 always returns False, even if "help" appears
    assert not is_greeting("can you please help me with my monthly report")


def test_greeting_reply_text():
    assert "Botree Insights Assistant" in GREETING_REPLY


# ============================================================
# formatting — rupees
# ============================================================


def test_format_rupees_indian_grouping_with_decimal():
    assert format_rupees(12345678.9) == "₹1,23,45,678.90"


def test_format_rupees_whole_number_no_decimal():
    assert format_rupees(250) == "₹250"
    assert format_rupees(25000) == "₹25,000"
    assert format_rupees(250000) == "₹2,50,000"


def test_format_rupees_negative():
    assert format_rupees(-500) == "-₹500"


def test_format_rupees_non_numeric_passthrough():
    assert format_rupees("N/A") == "N/A"


def test_is_money_column():
    assert is_money_column("TotalRevenue")
    assert is_money_column("gross_amt")
    assert not is_money_column("order_qty")
    assert not is_money_column("coverage_perc")
    assert not is_money_column("no_of_planned_outlets")


# ============================================================
# formatting — compute_total_facts
# ============================================================


def test_compute_total_facts_sums_additive_columns():
    columns = ["product_name", "TotalRevenue", "order_qty"]
    rows = [
        ["A", 100.0, 5],
        ["B", 200.0, 10],
        ["C", 300.0, 15],
    ]
    result = compute_total_facts(columns, rows)
    assert result["truncated"] is False
    assert result["row_count"] == 3
    assert set(result["target_columns"]) == {"TotalRevenue", "order_qty"}
    assert result["totals"]["TotalRevenue"] == 600.0
    assert result["totals"]["order_qty"] == 30


def test_compute_total_facts_truncated_when_at_row_cap():
    columns = ["TotalRevenue"]
    rows = [[float(i)] for i in range(50)]
    result = compute_total_facts(columns, rows, row_cap=50)
    assert result["truncated"] is True
    assert result["totals"] == {}


def test_compute_total_facts_falls_back_to_all_numeric_when_no_additive_match():
    columns = ["distance_covered"]
    rows = [[1.5], [2.5]]
    result = compute_total_facts(columns, rows)
    assert result["target_columns"] == ["distance_covered"]
    assert result["totals"]["distance_covered"] == 4.0


def test_compute_total_facts_empty_rows():
    result = compute_total_facts(["TotalRevenue"], [])
    assert result["totals"] == {}
    assert result["row_count"] == 0


# ============================================================
# formatting — YoY MTD detection
# ============================================================


def test_is_yoy_mtd_comparison_true():
    assert is_yoy_mtd_comparison("compare mtd sales to last year")
    assert is_yoy_mtd_comparison("mtd revenue year on year")


def test_is_yoy_mtd_comparison_requires_both_signals():
    assert not is_yoy_mtd_comparison("mtd sales")
    assert not is_yoy_mtd_comparison("compare to last year")
    assert not is_yoy_mtd_comparison("ytd sales vs last year")  # ytd, not mtd


def test_sql_rules_include_user_scope_my_zone_guidance():
    """Regression: the 'my/our scope is auto-applied — never invent geo values
    like My Zone' rule must be present (its absence caused empty results)."""
    from app.domain.sql_rules import SQL_RULES

    lowered = SQL_RULES.lower()
    assert "my zone" in lowered or "'my zone'" in lowered
    assert "automatically" in lowered
    assert "never invent" in lowered or "never add" in lowered


def test_cloudflare_prompt_includes_user_scope_guidance():
    """The ACTIVE Cloudflare/Llama prompt must carry the same scope guidance."""
    from app.llm.prompts import CLOUDFLARE_SQL_PROMPT

    lowered = CLOUDFLARE_SQL_PROMPT.lower()
    assert "my zone" in lowered
    assert "automatically" in lowered


def test_sql_rules_include_human_readable_guidance():
    """Regression: 'no of outlets…' once returned 50 raw unlabeled numbers.
    Rule 20 must demand ONE aggregated row, month names, and 'Unknown' labels."""
    lowered = SQL_RULES.lower()
    assert "one aggregated row" in lowered
    assert "'%m %y'" in lowered
    assert "coalesce(nullif" in lowered
    assert "no_of_ordered_outlets_without_to" in lowered


def test_sql_rules_region_only_in_dimension_tables():
    """Regression: 'region wise' grouped a fact table by State with NULL data
    because rule 3 claimed geo columns exist on every table."""
    lowered = SQL_RULES.lower()
    assert "geo_hier3_name" in lowered
    assert "only in distributor_t" in lowered


def test_cloudflare_prompt_includes_human_readable_guidance():
    """The ACTIVE Bedrock/Cloudflare condensed prompt must carry the same
    human-readable-output rules — it is the only prompt those providers see."""
    from app.llm.prompts import CLOUDFLARE_SQL_PROMPT

    lowered = CLOUDFLARE_SQL_PROMPT.lower()
    assert "one aggregated row" in lowered
    assert "'%m %y'" in lowered
    assert "coalesce(nullif" in lowered
    assert "no_of_ordered_outlets_without_to" in lowered
    assert "only in distributor_t" in lowered


def test_condensed_few_shots_reference_only_whitelisted_tables():
    from app.domain.sql_rules import CONDENSED_FEW_SHOT_EXAMPLES

    assert len(CONDENSED_FEW_SHOT_EXAMPLES) >= 5
    for example in CONDENSED_FEW_SHOT_EXAMPLES:
        sql = example["sql"]
        referenced = {table for table in INCLUDE_TABLES if table in sql}
        assert referenced, f"No whitelisted table referenced in: {sql}"


def test_few_shots_include_month_name_pattern():
    assert any(
        "DATE_FORMAT" in ex["sql"] and "'%M %Y'" in ex["sql"]
        for ex in FEW_SHOT_EXAMPLES
    )


def test_render_sample_rows_none_renders_blank():
    """A NULL cell must reach the answer LLM as '(blank)', never Python 'None'
    (a literal None made the model hallucinate a region name)."""
    from app.llm.prompts import render_sample_rows

    out = render_sample_rows([{"Region": None, "Total": 5}], ["Region", "Total"])
    assert "(blank)" in out
    assert "None" not in out
