"""RBAC row-scoping via sqlglot AST rewriting.

`apply_scope` injects per-user row filters (geo + sales-force hierarchy) into
every SELECT scope of a query, qualifying columns by each fact table's alias.
It ports the prototype's `inject_rbac_filters` semantics but does so on the AST
(the prototype spliced strings into a single WHERE — brittle and unsafe with
subqueries/unions). All literals are built through sqlglot constructors, so
scope values can never break out of their quoted context.

Fail-closed contract: a restricted user must never run unscoped SQL. If neither
geo nor hierarchy scope is available, or a fact table the query touches has no
applicable predicate column, or any AST manipulation fails, `RBACError` is
raised rather than returning partially-scoped SQL.

Canonical pipeline order (see `scoped_sql`):

    assert_safe(sql)  ->  apply_scope(tree, profile, subtree)  ->  enforce_limit
"""

from __future__ import annotations

import functools

import sqlglot
from sqlglot import exp

from app.domain.schema_catalog import FACT_TABLES, INCLUDE_TABLES, SCHEMA_DESCRIPTION
from app.errors import RBACError
from app.rbac.profiles import RBACProfile
from app.sqlsafety.guard import assert_safe
from app.sqlsafety.limiter import enforce_limit

# Fidelity / assumption note (verify against the LIVE MySQL schema):
# The fact tables list only the human-readable `sales_hierN_name` columns in
# SCHEMA_DESCRIPTION; the parallel `sales_hierN_code` columns the injector
# actually filters on are ASSUMED to exist whenever the matching `_name` is
# present (see `_supports_column`). Because every fix here is FAIL CLOSED, a
# WRONG assumption produces a BLOCKED query (safe over-block, friendly RBACError)
# — never an under-scoped/leaking query. If a `sales_hierN_code` column is later
# confirmed absent on a fact, the only effect is that scoped queries touching
# that level are refused, which is the correct secure default.

#: Sales-force level code (N00) -> the `sales_hierN_code` column used to filter
#: it. Ported verbatim from the prototype's `level_col_map`
#: (conversational_bot_v15.py ~l.509-516).
LEVEL_TO_HIER_COL: dict[int, str] = {
    100: "sales_hier1_code",
    200: "sales_hier2_code",
    300: "sales_hier3_code",
    400: "sales_hier4_code",
    500: "sales_hier5_code",
    600: "sales_hier6_code",
}

#: Whitelisted tables deliberately NOT scoped: they pass through unchanged even
#: for restricted users because they carry NO per-tenant sensitive rows and
#: expose NO geo/sales-hierarchy column to filter on.
#:
#: Per-table scoping decision for the non-fact whitelisted tables (columns taken
#: from SCHEMA_DESCRIPTION). Restricted users must NEVER receive out-of-scope
#: rows, so every table below is either SCOPED (predicate injected) or, when it
#: cannot be scoped by this geo/sales_hierN mechanism, FAILS CLOSED — the one
#: exception is the static global level lookup, which leaks nothing.
#:   - distributor_t           : geo_hier{1..7}_name + sales_hier{1..6}_code/_name
#:                               -> SCOPED (full geo + hierarchy).
#:   - rpt_customer_master_t    : geo_hier{2,4,6,7}_name + sales_hier{1..6}_name
#:                               -> SCOPED (geo + inferred sales_hierN_code).
#:   - salesman_t               : geo_hier{2,3,4,6,7}_name + sales_hier{1..6}_code/_name
#:                               -> SCOPED (geo + hierarchy).
#:   - sales_force_hier_value_t : sf_code, sf_name, sf_level_code, parent_code.
#:                               Holds SENSITIVE per-tenant org rows but exposes
#:                               no geo_hierN / sales_hierN column this injector
#:                               filters on -> it CANNOT be scoped here, so it is
#:                               NOT passed through: any restricted user with a
#:                               configured scope is blocked (RBACError). Scoping
#:                               it safely would need dedicated sf_code logic.
#:   - sales_force_hier_level_t : sf_level_code, sf_level_name, db_column_name.
#:                               A static, GLOBAL level lookup (6 rows: VP..SO),
#:                               identical for every tenant, with no geo/hierarchy
#:                               column and no per-tenant data -> JUSTIFIED
#:                               PASSTHROUGH (the only member of the set below).
UNSCOPED_REFERENCE_TABLES: frozenset[str] = frozenset({"sales_force_hier_level_t"})


# The Bisk Farm fact tables denormalize the FULL geo + sales hierarchies
# (levels 1..10, both `_name` and `_code`). This was VERIFIED against the live
# `rpt_invoice_summary_t` (145 columns) — every `geo_hierN_name/code` and
# `sales_hierN_name/code` for N in 1..10 is present. The static SCHEMA_DESCRIPTION
# lists only a representative subset (to keep the LLM prompt small), so for RBAC
# applicability we treat fact tables as carrying the whole family. This is a
# verified fact, not an assumption; the injector stays fail-closed for any
# column outside this family.
_HIER_FAMILY: frozenset[str] = frozenset(
    f"{dim}_hier{n}_{suffix}"
    for dim in ("geo", "sales")
    for n in range(1, 11)
    for suffix in ("name", "code")
)


def _table_columns(table_name: str) -> frozenset[str]:
    catalog = frozenset(SCHEMA_DESCRIPTION.get(table_name, {}).keys())
    if table_name in FACT_TABLES:
        return catalog | _HIER_FAMILY
    return catalog


def _supports_column(columns: frozenset[str], column: str) -> bool:
    """Whether a table (by its catalog columns) can be filtered on `column`.

    A `sales_hierN_code` column is treated as available whenever the table
    exposes the matching `sales_hierN_name` — fact tables list the human-readable
    name columns in the catalog while carrying the parallel code columns used for
    filtering.
    """
    if column in columns:
        return True
    if column.endswith("_code"):
        return column[: -len("_code")] + "_name" in columns
    return False


def _eq_or_in(col: exp.Expression, values: list[str]) -> exp.Expression:
    """`col = 'v'` for a single value, else `col IN ('v1', 'v2', ...)`."""
    literals = [exp.Literal.string(v) for v in values]
    if len(literals) == 1:
        return exp.EQ(this=col, expression=literals[0])
    return exp.In(this=col, expressions=literals)


def _effective_hier_map(
    profile: RBACProfile, subtree: dict[int, set[str]] | None
) -> dict[int, set[str]]:
    """Merge the user's own code (self at their level) with the subtree.

    Mirrors the prototype, which seeds the hierarchy filter with the user's own
    `sf_code` at their level and ORs in every descendant level.
    """
    merged: dict[int, set[str]] = {}
    if subtree:
        for level, codes in subtree.items():
            if codes:
                merged.setdefault(int(level), set()).update(str(c) for c in codes)
    if profile.sf_code and profile.sf_level is not None:
        merged.setdefault(int(profile.sf_level), set()).add(str(profile.sf_code))
    return merged


def _geo_predicate(
    profile: RBACProfile, qualifier: str, columns: frozenset[str]
) -> exp.Expression | None:
    if not (profile.geo_col and profile.geo_vals):
        return None
    if not _supports_column(columns, profile.geo_col):
        return None
    return _eq_or_in(exp.column(profile.geo_col, table=qualifier), list(profile.geo_vals))


def _hier_predicate(
    hier_map: dict[int, set[str]], qualifier: str, columns: frozenset[str]
) -> exp.Expression | None:
    parts: list[exp.Expression] = []
    for level in sorted(hier_map):
        column = LEVEL_TO_HIER_COL.get(level)
        if column is None or not _supports_column(columns, column):
            continue
        codes = sorted(hier_map[level])
        parts.append(_eq_or_in(exp.column(column, table=qualifier), codes))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return functools.reduce(exp.or_, parts)


def _table_predicate(
    table_name: str,
    qualifier: str,
    profile: RBACProfile,
    hier_map: dict[int, set[str]],
) -> exp.Expression | None:
    """Build the combined (geo AND hierarchy) predicate applicable to a table.

    Returns None when no part is applicable to this table's columns.
    """
    columns = _table_columns(table_name)
    parts: list[exp.Expression] = []
    geo = _geo_predicate(profile, qualifier, columns)
    if geo is not None:
        parts.append(geo)
    hier = _hier_predicate(hier_map, qualifier, columns)
    if hier is not None:
        parts.append(hier)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return exp.and_(*parts)


def _direct_tables(select: exp.Select) -> list[exp.Table]:
    """Tables referenced directly in a select's FROM/JOINs (not in subqueries)."""
    tables: list[exp.Table] = []
    from_ = select.args.get("from_")
    if from_ is not None and isinstance(from_.this, exp.Table):
        tables.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def _as_tree(sql_or_tree: str | exp.Expression) -> exp.Expression:
    if isinstance(sql_or_tree, exp.Expression):
        return sql_or_tree.copy()
    return sqlglot.parse_one(sql_or_tree, dialect="mysql")


def _scope_table(
    select: exp.Select,
    table: exp.Table,
    profile: RBACProfile,
    hier_map: dict[int, set[str]],
    geo_available: bool,
) -> None:
    """Inject the RBAC predicate for one directly-referenced base table.

    Fail-closed enforcement (IMPORTANT-2): every scope dimension the profile has
    *configured* must be applicable to this table. If a configured dimension
    cannot be enforced (the table lacks that column), silently omitting it would
    widen visibility, so we raise RBACError instead. Over-blocking with a
    friendly error is the correct secure default over leaking rows.
    """
    columns = _table_columns(table.name)
    qualifier = table.alias_or_name

    # Configured geo must be enforceable on this table.
    if geo_available and not _supports_column(columns, profile.geo_col):
        raise RBACError(
            f"Configured geo scope ('{profile.geo_col}') cannot be enforced on "
            f"table '{table.name}'; refusing to run under-scoped SQL."
        )
    # Configured hierarchy must have at least one applicable level column.
    if hier_map and _hier_predicate(hier_map, qualifier, columns) is None:
        raise RBACError(
            f"Configured hierarchy scope cannot be enforced on table "
            f"'{table.name}'; refusing to run under-scoped SQL."
        )

    predicate = _table_predicate(table.name, qualifier, profile, hier_map)
    if predicate is None:
        raise RBACError(
            f"No applicable RBAC predicate for table '{table.name}'; "
            f"refusing to run unscoped SQL."
        )
    select.where(predicate, copy=False)


def apply_scope(
    sql_or_tree: str | exp.Expression,
    profile: RBACProfile,
    subtree: dict[int, set[str]] | None,
) -> str:
    """Inject RBAC row filters and return the scoped SQL string.

    Unrestricted profiles get the SQL back unchanged (normalized through a
    sqlglot round-trip). For restricted profiles, EVERY directly-referenced
    whitelisted base table in EVERY SELECT scope (FROM/JOINs, including each
    UNION branch and each derived-table inner select) is scoped with its own
    geo + hierarchy predicate — no table is trusted to be scoped implicitly by a
    join partner. Tables in `UNSCOPED_REFERENCE_TABLES` pass through by
    documented decision; every other scopable table that a configured dimension
    cannot be enforced on fails closed (see module docstring and `_scope_table`).
    """
    tree = _as_tree(sql_or_tree)

    if profile.is_unrestricted:
        return tree.sql(dialect="mysql")

    hier_map = _effective_hier_map(profile, subtree)
    geo_available = bool(profile.geo_col and profile.geo_vals)
    if not geo_available and not hier_map:
        raise RBACError(
            "Restricted user has neither geo nor hierarchy scope; refusing to run unscoped SQL."
        )

    try:
        for select in list(tree.find_all(exp.Select)):
            for table in _direct_tables(select):
                name = table.name
                if name not in INCLUDE_TABLES:
                    # CTE self-reference or non-base identifier — nothing to scope.
                    continue
                if name in UNSCOPED_REFERENCE_TABLES:
                    continue  # documented justified passthrough (see set docstring)
                _scope_table(select, table, profile, hier_map, geo_available)
    except RBACError:
        raise
    except Exception as exc:  # any AST failure fails closed
        raise RBACError(f"RBAC scope injection failed: {exc}") from exc

    return tree.sql(dialect="mysql")


def scoped_sql(
    sql: str,
    profile: RBACProfile,
    subtree: dict[int, set[str]] | None,
    cap: int = 50,
) -> str:
    """Convenience wrapper: guard, then scope, then cap.

    Canonical order: `assert_safe` (parse + whitelist) -> `apply_scope` (RBAC) ->
    `enforce_limit` (outer cap). The alias fixer is intentionally NOT run here;
    the generation/self-correction loop decides whether to fix before calling.
    """
    assert_safe(sql)
    scoped = apply_scope(sql, profile, subtree)
    capped = enforce_limit(sqlglot.parse_one(scoped, dialect="mysql"), cap)
    return capped.sql(dialect="mysql")


__all__ = [
    "LEVEL_TO_HIER_COL",
    "UNSCOPED_REFERENCE_TABLES",
    "apply_scope",
    "scoped_sql",
]
