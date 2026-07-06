"""Parse-time SQL guard.

`assert_safe` parses a candidate SQL string with sqlglot (MySQL dialect) and
raises `SQLSafetyError` on ANY violation. It fails closed: anything it cannot
prove safe is blocked. On success it returns the parsed AST so callers can
reuse it (RBAC injection, limit enforcement) without re-parsing.

Design: this is a whitelist. Only single read-only SELECT / UNION statements
against whitelisted tables are allowed. It replaces the prototype's
`is_safe_sql` (which did substring matching on the upper-cased SQL text — a
regex approach defeated by comment obfuscation). Verdicts here come from the
AST, never from string scanning.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.domain.schema_catalog import INCLUDE_TABLES
from app.errors import SQLSafetyError

#: Statement/clause node types that must never appear anywhere in the tree.
#: The top-level statement is separately constrained to SELECT/UNION, so these
#: are defence-in-depth against nested or smuggled DML/DDL/administrative nodes.
_FORBIDDEN_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Set,
    exp.Use,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Merge,
    exp.LoadData,
    exp.Into,  # SELECT ... INTO OUTFILE / DUMPFILE / INTO @var
    exp.Lock,  # SELECT ... FOR UPDATE / LOCK IN SHARE MODE
)

#: Function names (case-insensitive) that are dangerous regardless of context.
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "SLEEP",
        "BENCHMARK",
        "LOAD_FILE",
        "GET_LOCK",
        "RELEASE_LOCK",
        "UPDATEXML",
        "EXTRACTVALUE",
        "SYS_EVAL",
        "SYS_EXEC",
    }
)


def _parse_single(sql: str) -> exp.Expression:
    """Parse `sql` and return the single top-level statement, or raise."""
    if not sql or not isinstance(sql, str) or not sql.strip():
        raise SQLSafetyError("Empty SQL statement is not allowed.")

    try:
        statements = sqlglot.parse(sql, dialect="mysql")
    except SqlglotError as exc:
        raise SQLSafetyError(f"SQL could not be parsed and was blocked: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive; any parser fault fails closed
        raise SQLSafetyError("SQL could not be parsed and was blocked.") from exc

    real = [s for s in statements if s is not None]
    if len(real) != 1:
        raise SQLSafetyError(
            f"Exactly one statement is allowed; found {len(real)}. Multi-statement SQL is blocked."
        )
    return real[0]


def _function_name(node: exp.Expression) -> str | None:
    """Best-effort function name for a call node, upper-cased."""
    if isinstance(node, exp.Anonymous):
        name = node.name
        return name.upper() if name else None
    if isinstance(node, exp.Func):
        try:
            name = node.sql_name()
        except Exception:  # pragma: no cover - defensive
            name = node.name
        return name.upper() if name else None
    return None


def assert_safe(sql: str) -> exp.Expression:
    """Validate `sql` and return its parsed AST, or raise `SQLSafetyError`.

    Guarantees on the returned tree:
      * exactly one statement, a SELECT or UNION-of-SELECTs (CTEs allowed);
      * no DML/DDL/administrative nodes, no INTO OUTFILE/DUMPFILE, no locking;
      * no dangerous functions (SLEEP/BENCHMARK/LOAD_FILE/…);
      * every referenced table is in the whitelist and not schema-qualified.
    """
    tree = _parse_single(sql)

    # Top-level statement must be a SELECT or a UNION of selects. CTE queries
    # (`WITH ... SELECT`) parse as a Select/Union with a `with` arg, so they
    # are covered by this check too.
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLSafetyError(
            f"Only SELECT queries are allowed; got a {type(tree).__name__} statement."
        )

    # Collect CTE alias names first — these are self-references, not real tables.
    cte_names = {cte.alias for cte in tree.find_all(exp.CTE) if cte.alias}

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise SQLSafetyError(f"Disallowed SQL construct ({type(node).__name__}) is blocked.")

        fname = _function_name(node)
        if fname is not None and fname in _FORBIDDEN_FUNCTIONS:
            raise SQLSafetyError(f"Disallowed function {fname}() is blocked.")

        if isinstance(node, exp.Table):
            _assert_table_allowed(node, cte_names)

    return tree


def _assert_table_allowed(table: exp.Table, cte_names: set[str]) -> None:
    """Raise unless `table` is a plain reference to a whitelisted table."""
    name = table.name

    # A reference to a CTE defined in this query (and not schema-qualified).
    if name in cte_names and not table.db and not table.catalog:
        return

    if table.catalog or table.db:
        # Schema/catalog qualification (e.g. `mysql.user`,
        # `information_schema.tables`) is never allowed.
        qualified = ".".join(p for p in (table.catalog, table.db, name) if p)
        raise SQLSafetyError(f"Schema-qualified table reference '{qualified}' is blocked.")

    if name not in INCLUDE_TABLES:
        raise SQLSafetyError(f"Table '{name}' is not in the allowed table list.")


__all__ = ["assert_safe"]
