"""Tests for RBAC scope injection, hierarchy resolution, and profiles.

Covers TEST_PLAN RBAC-01..10 and RBAC-12. Golden SQL comparisons normalize
both sides through sqlglot so formatting differences never cause spurious
failures.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from app.errors import RBACError
from app.rbac.hierarchy import clear_cache, get_subtree_for, resolve_subtree
from app.rbac.injector import apply_scope, scoped_sql
from app.rbac.profiles import RBACProfile, profile_from_user, rbac_fingerprint


def norm(sql: str) -> str:
    return sqlglot.parse_one(sql, dialect="mysql").sql(dialect="mysql")


# ---- profile fixtures -------------------------------------------------------

VP = RBACProfile(user_id="u-vp", role="VP", sf_level=100, sf_code="100", geo_col=None, geo_vals=())
SO = RBACProfile(
    user_id="u-so",
    role="SO",
    sf_level=600,
    sf_code="SO123",
    geo_col="geo_hier7_name",
    geo_vals=("Trichy Town",),
)
ASM = RBACProfile(
    user_id="u-asm",
    role="ASM",
    sf_level=500,
    sf_code="ASM1",
    geo_col="geo_hier6_name",
    geo_vals=("CHENNAI District",),
)


# ============================================================
# RBAC-01: VP passthrough
# ============================================================


def test_rbac_01_vp_passthrough_unchanged() -> None:
    sql = "SELECT SUM(measure_14) FROM rpt_invoice_summary_t inv WHERE inv.invoice_status = 'Delivered'"
    assert norm(apply_scope(sql, VP, {})) == norm(sql)


def test_rbac_01_unrestricted_by_level_even_if_role_differs() -> None:
    prof = RBACProfile("u", "ADMIN", 100, "100", "geo_hier2_name", ("SOUTH",))
    sql = "SELECT * FROM rpt_invoice_summary_t"
    assert norm(apply_scope(sql, prof, {})) == norm(sql)


# ============================================================
# RBAC-02: SO geo + hierarchy on a plain fact query
# ============================================================


def test_rbac_02_so_geo_and_hierarchy_predicates() -> None:
    out = apply_scope("SELECT SUM(measure_14) FROM rpt_invoice_summary_t inv", SO, {})
    expected = (
        "SELECT SUM(measure_14) FROM rpt_invoice_summary_t AS inv "
        "WHERE inv.geo_hier7_name = 'Trichy Town' AND inv.sales_hier6_code = 'SO123'"
    )
    assert norm(out) == norm(expected)


# ============================================================
# RBAC-03: existing WHERE is ANDed, not replaced
# ============================================================


def test_rbac_03_existing_where_is_anded() -> None:
    out = apply_scope(
        "SELECT * FROM rpt_invoice_summary_t inv WHERE inv.invoice_status = 'Delivered'",
        SO,
        {},
    )
    expected = (
        "SELECT * FROM rpt_invoice_summary_t AS inv "
        "WHERE inv.invoice_status = 'Delivered' "
        "AND (inv.geo_hier7_name = 'Trichy Town' AND inv.sales_hier6_code = 'SO123')"
    )
    assert norm(out) == norm(expected)


# ============================================================
# RBAC-04: aliased fact qualified via alias
# ============================================================


def test_rbac_04_predicate_uses_table_alias() -> None:
    out = apply_scope("SELECT measure_14 FROM rpt_invoice_summary_t inv", SO, {})
    tree = sqlglot.parse_one(out, dialect="mysql")
    cols = [c for c in tree.find_all(exp.Column) if c.name == "geo_hier7_name"]
    assert cols and all(c.table == "inv" for c in cols)


# ============================================================
# RBAC-05: fact inside a derived table gets scoped inside the inner SELECT
# ============================================================


def test_rbac_05_derived_table_scoped_inside() -> None:
    out = apply_scope("SELECT * FROM (SELECT * FROM rpt_order_summary_t o) sub", SO, {})
    expected = (
        "SELECT * FROM (SELECT * FROM rpt_order_summary_t AS o "
        "WHERE o.geo_hier7_name = 'Trichy Town' AND o.sales_hier6_code = 'SO123') AS sub"
    )
    assert norm(out) == norm(expected)


# ============================================================
# RBAC-06: every UNION branch touching a fact gets the predicate
# ============================================================


def test_rbac_06_union_both_branches_scoped() -> None:
    out = apply_scope(
        "SELECT measure_14 FROM rpt_invoice_summary_t "
        "UNION SELECT order_value FROM rpt_order_summary_t",
        SO,
        {},
    )
    expected = (
        "SELECT measure_14 FROM rpt_invoice_summary_t "
        "WHERE rpt_invoice_summary_t.geo_hier7_name = 'Trichy Town' "
        "AND rpt_invoice_summary_t.sales_hier6_code = 'SO123' "
        "UNION SELECT order_value FROM rpt_order_summary_t "
        "WHERE rpt_order_summary_t.geo_hier7_name = 'Trichy Town' "
        "AND rpt_order_summary_t.sales_hier6_code = 'SO123'"
    )
    assert norm(out) == norm(expected)


# ============================================================
# RBAC-07: dimension-only query — distributor_t scoped per ported policy
# ============================================================


def test_rbac_07_dimension_only_distributor_scoped() -> None:
    out = apply_scope("SELECT name FROM distributor_t d", SO, {})
    expected = (
        "SELECT name FROM distributor_t AS d "
        "WHERE d.geo_hier7_name = 'Trichy Town' AND d.sales_hier6_code = 'SO123'"
    )
    assert norm(out) == norm(expected)


def test_rbac_07_salesman_dimension_is_scoped_not_passthrough() -> None:
    # SECURITY: salesman_t carries geo_hier7_name + sales_hier6_code, so a
    # restricted SO must get a predicate injected — NOT passed through unchanged
    # (the old test asserted the cross-tenant-leak bug).
    out = apply_scope("SELECT name FROM salesman_t", SO, {})
    expected = (
        "SELECT name FROM salesman_t "
        "WHERE salesman_t.geo_hier7_name = 'Trichy Town' "
        "AND salesman_t.sales_hier6_code = 'SO123'"
    )
    assert norm(out) == norm(expected)


# ============================================================
# CRITICAL-1: non-distributor dimension tables are scoped, not leaked
# ============================================================


def test_so_customer_master_alone_is_scoped() -> None:
    out = apply_scope("SELECT customer_name FROM rpt_customer_master_t", SO, {})
    tree = sqlglot.parse_one(out, dialect="mysql")
    geo = [c for c in tree.find_all(exp.Column) if c.name == "geo_hier7_name"]
    hier = [c for c in tree.find_all(exp.Column) if c.name == "sales_hier6_code"]
    assert geo and hier  # both scope predicates present
    assert norm(out) != norm("SELECT customer_name FROM rpt_customer_master_t")


def test_so_union_fact_and_customer_master_both_branches_scoped() -> None:
    out = apply_scope(
        "SELECT retailer_name FROM rpt_invoice_summary_t "
        "UNION SELECT customer_name FROM rpt_customer_master_t",
        SO,
        {},
    )
    tree = sqlglot.parse_one(out, dialect="mysql")
    # Every SELECT branch must carry a WHERE clause with the geo predicate.
    selects = list(tree.find_all(exp.Select))
    assert len(selects) == 2
    for sel in selects:
        assert sel.args.get("where") is not None
    geo_cols = [c for c in tree.find_all(exp.Column) if c.name == "geo_hier7_name"]
    assert len(geo_cols) == 2  # one per branch


def test_so_hier_value_table_fails_closed() -> None:
    # sales_force_hier_value_t exposes no geo/sales_hierN column the injector can
    # filter on, but holds sensitive per-tenant rows → must fail closed, never
    # pass through.
    with pytest.raises(RBACError):
        apply_scope("SELECT sf_name FROM sales_force_hier_value_t", SO, {})


def test_so_hier_level_lookup_passes_through() -> None:
    # sales_force_hier_level_t is a static global level lookup with no per-tenant
    # rows → documented justified passthrough.
    sql = "SELECT sf_level_name FROM sales_force_hier_level_t"
    assert norm(apply_scope(sql, SO, {})) == norm(sql)


def test_configured_geo_missing_on_table_fails_closed() -> None:
    # Restricted profile whose geo column is NOT part of the denormalized
    # hierarchy family a fact table carries → un-enforceable configured
    # dimension → RBACError (fail closed). (All fact tables carry the full
    # geo_hier1..10 / sales_hier1..10 family, verified against the live DB, so
    # a genuinely-absent geo column is one outside that family.)
    prof = RBACProfile(
        "u", "SO", 600, "SO123", "territory_zone_name", ("Trichy Town",)
    )
    with pytest.raises(RBACError):
        apply_scope("SELECT measure_14 FROM rpt_invoice_summary_t", prof, {})


def test_executable_comment_neutralized_in_render() -> None:
    # MINOR-3: a MySQL executable comment /*!...*/ must not survive into the
    # injector's re-rendered output as an executable-comment sequence.
    out = apply_scope(
        "SELECT /*!50000 measure_14 */ measure_14 FROM rpt_invoice_summary_t", SO, {}
    )
    assert "/*!" not in out


# ============================================================
# RBAC subtree merge into hierarchy predicate
# ============================================================


def test_asm_subtree_ors_child_levels() -> None:
    out = apply_scope("SELECT * FROM rpt_invoice_summary_t", ASM, {600: {"S1", "S2"}})
    expected = (
        "SELECT * FROM rpt_invoice_summary_t "
        "WHERE rpt_invoice_summary_t.geo_hier6_name = 'CHENNAI District' "
        "AND (rpt_invoice_summary_t.sales_hier5_code = 'ASM1' "
        "OR rpt_invoice_summary_t.sales_hier6_code IN ('S1', 'S2'))"
    )
    assert norm(out) == norm(expected)


# ============================================================
# RBAC-08: string values are AST-escaped, no injection
# ============================================================


def test_rbac_08_malicious_geo_value_is_escaped_literal() -> None:
    evil = RBACProfile("u", "SO", 600, "S1", "geo_hier7_name", ("x' OR '1'='1",))
    out = apply_scope("SELECT * FROM rpt_invoice_summary_t", evil, {})
    # Output must still be valid, single-statement SQL.
    statements = sqlglot.parse(out, dialect="mysql")
    assert len(statements) == 1
    tree = statements[0]
    # The geo predicate must be a plain equality against a string literal whose
    # value is the raw input — not injected SQL structure.
    geo_eqs = [
        node
        for node in tree.find_all(exp.EQ)
        if isinstance(node.this, exp.Column) and node.this.name == "geo_hier7_name"
    ]
    assert len(geo_eqs) == 1
    rhs = geo_eqs[0].expression
    assert isinstance(rhs, exp.Literal) and rhs.is_string
    assert rhs.this == "x' OR '1'='1"


# ============================================================
# RBAC-09: fail closed on a fact with no applicable predicate
# ============================================================


def test_rbac_09_fact_without_applicable_predicate_raises() -> None:
    # geo_col does not exist on the fact and there is no hierarchy scope → no
    # part applies → fail closed rather than run unscoped.
    prof = RBACProfile("u", "BM", 400, None, "nonexistent_geo_col", ("v",))
    with pytest.raises(RBACError):
        apply_scope("SELECT * FROM rpt_invoice_summary_t", prof, {})


def test_restricted_user_with_empty_scope_raises() -> None:
    prof = RBACProfile("u", "SO", 600, None, None, ())
    with pytest.raises(RBACError):
        apply_scope("SELECT * FROM rpt_invoice_summary_t", prof, {})


# ============================================================
# RBAC-10: fingerprint stability / difference
# ============================================================


def test_rbac_10_fingerprint_stable_across_user_id() -> None:
    a = RBACProfile("user-a", "SO", 600, "SO123", "geo_hier7_name", ("Trichy Town",))
    b = RBACProfile("user-b", "SO", 600, "SO123", "geo_hier7_name", ("Trichy Town",))
    assert rbac_fingerprint(a) == rbac_fingerprint(b)


def test_rbac_10_fingerprint_stable_regardless_of_geo_order() -> None:
    a = RBACProfile("u", "ZSM", 200, "Z1", "geo_hier2_name", ("SOUTH", "EAST"))
    b = RBACProfile("u", "ZSM", 200, "Z1", "geo_hier2_name", ("EAST", "SOUTH"))
    assert rbac_fingerprint(a) == rbac_fingerprint(b)


def test_rbac_10_fingerprint_differs_across_scope() -> None:
    assert rbac_fingerprint(SO) != rbac_fingerprint(VP)
    assert rbac_fingerprint(SO) != rbac_fingerprint(ASM)


# ============================================================
# profile_from_user
# ============================================================


def test_profile_from_user_maps_fields_and_tuple_geo() -> None:
    user = SimpleNamespace(
        id="abc",
        role="SO",
        sf_level=600,
        sf_code="SO123",
        allowed_geo_col="geo_hier7_name",
        allowed_geo_vals=["Trichy Town"],
    )
    prof = profile_from_user(user)
    assert prof.user_id == "abc"
    assert prof.geo_vals == ("Trichy Town",)
    assert not prof.is_unrestricted


def test_profile_from_user_none_geo_becomes_empty_tuple() -> None:
    user = SimpleNamespace(
        id="v",
        role="VP",
        sf_level=100,
        sf_code="100",
        allowed_geo_col=None,
        allowed_geo_vals=None,
    )
    prof = profile_from_user(user)
    assert prof.geo_vals == ()
    assert prof.is_unrestricted


# ============================================================
# RBAC-12: hierarchy subtree resolver
# ============================================================

# VP(100) -> ZSM(200) -> RSM(300) -> BM(400) -> ASM(500) -> SO(600), plus a
# sibling SO(601) under the same ASM.
HIER_ROWS = [
    {"sf_code": "100", "sf_name": "VP", "sf_level_code": "100", "parent_code": None},
    {"sf_code": "200", "sf_name": "ZSM", "sf_level_code": "200", "parent_code": "100"},
    {"sf_code": "300", "sf_name": "RSM", "sf_level_code": "300", "parent_code": "200"},
    {"sf_code": "400", "sf_name": "BM", "sf_level_code": "400", "parent_code": "300"},
    {"sf_code": "500", "sf_name": "ASM", "sf_level_code": "500", "parent_code": "400"},
    {"sf_code": "600", "sf_name": "SO", "sf_level_code": "600", "parent_code": "500"},
    {"sf_code": "601", "sf_name": "SO2", "sf_level_code": "600", "parent_code": "500"},
]


def test_rbac_12_so_leaf_has_empty_subtree() -> None:
    assert resolve_subtree(HIER_ROWS, "600") == {}


def test_rbac_12_asm_sees_child_sos() -> None:
    assert resolve_subtree(HIER_ROWS, "500") == {600: {"600", "601"}}


def test_rbac_12_bm_sees_asm_and_sos() -> None:
    assert resolve_subtree(HIER_ROWS, "400") == {500: {"500"}, 600: {"600", "601"}}


def test_rbac_12_zsm_sees_full_chain() -> None:
    assert resolve_subtree(HIER_ROWS, "200") == {
        300: {"300"},
        400: {"400"},
        500: {"500"},
        600: {"600", "601"},
    }


def test_rbac_12_unknown_root_empty() -> None:
    assert resolve_subtree(HIER_ROWS, "does-not-exist") == {}


# ============================================================
# get_subtree_for — injected fetcher + TTL cache
# ============================================================


async def test_get_subtree_for_uses_injected_fetcher_and_caches() -> None:
    clear_cache()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return HIER_ROWS

    prof = RBACProfile("u", "ASM", 500, "500", "geo_hier6_name", ("CHENNAI District",))
    first = await get_subtree_for(prof, fetch)
    second = await get_subtree_for(prof, fetch)
    assert first == {600: {"600", "601"}}
    assert second == first
    assert calls["n"] == 1  # second call served from cache
    clear_cache()


async def test_get_subtree_for_unrestricted_returns_empty_without_fetch() -> None:
    clear_cache()

    async def fetch():  # pragma: no cover - must not be called
        raise AssertionError("fetcher should not run for unrestricted profile")

    assert await get_subtree_for(VP, fetch) == {}


# ============================================================
# scoped_sql — full guard -> scope -> limit pipeline
# ============================================================


def test_scoped_sql_pipeline_scopes_and_caps() -> None:
    out = scoped_sql("SELECT SUM(measure_14) FROM rpt_invoice_summary_t", SO, {})
    expected = (
        "SELECT SUM(measure_14) FROM rpt_invoice_summary_t "
        "WHERE rpt_invoice_summary_t.geo_hier7_name = 'Trichy Town' "
        "AND rpt_invoice_summary_t.sales_hier6_code = 'SO123' LIMIT 50"
    )
    assert norm(out) == norm(expected)


def test_scoped_sql_blocks_unsafe_before_scoping() -> None:
    from app.errors import SQLSafetyError

    with pytest.raises(SQLSafetyError):
        scoped_sql("DROP TABLE distributor_t", SO, {})
