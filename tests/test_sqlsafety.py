"""Tests for the SQL safety core: guard, limiter, fixer (TEST_PLAN SAFE-01..13)."""

from __future__ import annotations

import pytest
import sqlglot

from app.errors import SQLSafetyError
from app.sqlsafety import assert_safe, enforce_limit, fix_column_aliases, tree_to_sql


def norm(sql: str) -> str:
    return sqlglot.parse_one(sql, dialect="mysql").sql(dialect="mysql")


# ============================================================
# guard — allowed queries (SAFE-01)
# ============================================================

ALLOWED_SQL = [
    "SELECT SUM(measure_14) FROM rpt_invoice_summary_t",
    "SELECT d.name, i.measure_14 FROM distributor_t d "
    "JOIN rpt_invoice_summary_t i ON i.distributor_code = d.code",
    "SELECT code FROM distributor_t WHERE lob_code = 'X' GROUP BY code ORDER BY code",
    "WITH x AS (SELECT code FROM distributor_t) SELECT * FROM x",
    "SELECT measure_14 FROM rpt_invoice_summary_t "
    "UNION SELECT order_value FROM rpt_order_summary_t",
    # inline comments must not change the (safe) verdict — AST, not regex
    "SELECT/**/measure_14/**/FROM/**/rpt_invoice_summary_t",
    "SELECT measure_14 FROM rpt_invoice_summary_t -- trailing comment",
]


@pytest.mark.parametrize("sql", ALLOWED_SQL)
def test_safe_01_allowed_select_passes(sql: str) -> None:
    tree = assert_safe(sql)
    assert tree is not None


# ============================================================
# guard — blocked queries (SAFE-02..07, 12, 13)
# ============================================================

BLOCKED_SQL = [
    # SAFE-02: DML/DDL
    "INSERT INTO distributor_t (code) VALUES ('x')",
    "UPDATE distributor_t SET name = 'x'",
    "DELETE FROM distributor_t",
    "DROP TABLE distributor_t",
    "TRUNCATE TABLE distributor_t",
    "ALTER TABLE distributor_t ADD COLUMN x INT",
    "CREATE TABLE evil (id INT)",
    # SAFE-03: multi-statement
    "SELECT 1 FROM distributor_t; DROP TABLE distributor_t",
    "SELECT 1 FROM distributor_t; SELECT 2 FROM salesman_t",
    # SAFE-04: non-whitelisted / schema-qualified
    "SELECT * FROM users",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM mysql.user",
    "SELECT * FROM performance_schema.threads",
    # SAFE-05: file / timing abuse
    "SELECT * FROM rpt_order_summary_t INTO OUTFILE '/tmp/x'",
    "SELECT LOAD_FILE('/etc/passwd') FROM distributor_t",
    "SELECT SLEEP(5) FROM distributor_t",
    "SELECT BENCHMARK(1000000, MD5('a')) FROM distributor_t",
    "SELECT GET_LOCK('a', 1) FROM distributor_t",
    "SELECT measure_14 FROM rpt_invoice_summary_t WHERE UPDATEXML(1, 2, 3) = 1",
    "SELECT measure_14 FROM rpt_invoice_summary_t WHERE EXTRACTVALUE(1, 2) = 1",
    # SAFE-06: SET / USE / SHOW / EXPLAIN (SELECT-only)
    "SET @x = 1",
    "USE mydb",
    "SHOW TABLES",
    "EXPLAIN SELECT 1 FROM distributor_t",
    # SAFE-07: comment-obfuscated keyword — AST rejects it
    "SEL/**/ECT 1 FROM distributor_t",
    # SAFE-12: unparseable garbage
    "not sql at all ~~~ >>>",
    "",
    # locking
    "SELECT * FROM distributor_t FOR UPDATE",
    "SELECT * FROM distributor_t LOCK IN SHARE MODE",
    # SAFE-13: UNION smuggling a non-whitelisted table
    "SELECT code FROM distributor_t UNION SELECT user FROM mysql.user",
    "SELECT code FROM distributor_t UNION SELECT id FROM users",
]


@pytest.mark.parametrize("sql", BLOCKED_SQL)
def test_guard_blocks(sql: str) -> None:
    with pytest.raises(SQLSafetyError):
        assert_safe(sql)


def test_safe_04_cte_self_reference_not_treated_as_table() -> None:
    # `x` is a CTE alias, not a base table — must not trip the whitelist.
    assert_safe("WITH x AS (SELECT code FROM distributor_t) SELECT * FROM x")


# ============================================================
# limiter (SAFE-08..10)
# ============================================================


def test_safe_08_adds_limit_when_absent() -> None:
    out = tree_to_sql(enforce_limit(assert_safe("SELECT a FROM distributor_t")))
    assert norm(out) == norm("SELECT a FROM distributor_t LIMIT 50")


def test_safe_09_clamps_limit_above_cap() -> None:
    out = tree_to_sql(enforce_limit(assert_safe("SELECT a FROM distributor_t LIMIT 5000")))
    assert norm(out) == norm("SELECT a FROM distributor_t LIMIT 50")


def test_safe_09_leaves_limit_below_cap() -> None:
    out = tree_to_sql(enforce_limit(assert_safe("SELECT a FROM distributor_t LIMIT 10")))
    assert norm(out) == norm("SELECT a FROM distributor_t LIMIT 10")


def test_safe_09_leaves_limit_equal_cap() -> None:
    out = tree_to_sql(enforce_limit(assert_safe("SELECT a FROM distributor_t LIMIT 50")))
    assert norm(out) == norm("SELECT a FROM distributor_t LIMIT 50")


def test_safe_10_subquery_limit_untouched_outer_added() -> None:
    sql = "SELECT a FROM (SELECT b FROM distributor_t LIMIT 3) x"
    out = tree_to_sql(enforce_limit(assert_safe(sql), cap=50))
    assert norm(out) == norm("SELECT a FROM (SELECT b FROM distributor_t LIMIT 3) x LIMIT 50")


def test_limiter_union_outer_limit() -> None:
    sql = "SELECT a FROM distributor_t UNION SELECT b FROM salesman_t"
    out = tree_to_sql(enforce_limit(assert_safe(sql)))
    assert norm(out) == norm("SELECT a FROM distributor_t UNION SELECT b FROM salesman_t LIMIT 50")


def test_limiter_union_clamps_outer_limit() -> None:
    sql = "SELECT a FROM distributor_t UNION SELECT b FROM salesman_t LIMIT 9000"
    out = tree_to_sql(enforce_limit(assert_safe(sql)))
    assert norm(out) == norm("SELECT a FROM distributor_t UNION SELECT b FROM salesman_t LIMIT 50")


# ============================================================
# fixer (SAFE-11)
# ============================================================


def test_safe_11_fixes_wrong_alias_for_unique_column() -> None:
    # measure_14 is unique to rpt_invoice_summary_t (and not ambiguous), so a
    # wrong `d.` qualifier is re-pointed to the invoice table's alias.
    sql = (
        "SELECT d.measure_14 FROM distributor_t d "
        "JOIN rpt_invoice_summary_t inv ON inv.distributor_code = d.code"
    )
    out = fix_column_aliases(sql)
    assert norm(out) == norm(
        "SELECT inv.measure_14 FROM distributor_t d "
        "JOIN rpt_invoice_summary_t inv ON inv.distributor_code = d.code"
    )


def test_safe_11_leaves_ambiguous_column_qualifier_alone() -> None:
    # invoice_number is in AMBIGUOUS_COLUMNS — never auto-rewritten.
    sql = (
        "SELECT d.invoice_number FROM distributor_t d "
        "JOIN rpt_invoice_summary_t inv ON inv.distributor_code = d.code"
    )
    assert fix_column_aliases(sql) == sql


def test_fixer_leaves_ambiguous_column_alone() -> None:
    sql = "SELECT rcp.customer_code FROM rpt_route_coverage_plan_t rcp"
    assert fix_column_aliases(sql) == sql


def test_fixer_leaves_correctly_qualified_column() -> None:
    sql = "SELECT inv.invoice_number FROM rpt_invoice_summary_t inv"
    assert fix_column_aliases(sql) == sql


def test_fixer_unfixable_when_correct_table_absent() -> None:
    # measure_14 belongs to rpt_invoice_summary_t, which is not in the query, so
    # there is nowhere valid to re-point it → leave unchanged.
    sql = "SELECT d.measure_14 FROM distributor_t d"
    assert fix_column_aliases(sql) == sql


def test_fixer_never_raises_on_garbage() -> None:
    assert fix_column_aliases("not valid sql ~~~") == "not valid sql ~~~"
