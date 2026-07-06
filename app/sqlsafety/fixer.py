"""Best-effort column↔alias reconciliation.

Ports the prototype's `validate_and_fix_sql` onto a sqlglot AST. When the SQL
generator qualifies a column with the wrong table alias (e.g. `d.invoice_number`
where `invoice_number` actually lives on `rpt_invoice_summary_t`), this
re-qualifies it to the correct table — but only when that table is present in
the query and the column unambiguously belongs to it.

This is a pure best-effort helper: it MUST NEVER raise. If the SQL cannot be
parsed, or a reference cannot be confidently fixed, the input is returned
unchanged and the safety guard / execution error surfaces to the caller's
self-correction loop instead.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.domain.schema_catalog import (
    ALIAS_TABLE_MAP,
    AMBIGUOUS_COLUMNS,
    COLUMN_TABLE_MAP,
)


def fix_column_aliases(sql: str) -> str:
    """Return `sql` with wrong column qualifiers re-pointed to the right table.

    Never raises; returns the input unchanged if it cannot be parsed or fixed.
    """
    if not sql or not isinstance(sql, str):
        return sql

    try:
        tree = sqlglot.parse_one(sql, dialect="mysql")
    except Exception:
        return sql

    try:
        # qualifier (alias or bare table name used in the query) -> real table
        query_alias_map: dict[str, str] = {}
        tables_present: set[str] = set()
        for table in tree.find_all(exp.Table):
            tables_present.add(table.name)
            query_alias_map[table.alias_or_name] = table.name

        changed = False
        for column in tree.find_all(exp.Column):
            qualifier = column.table
            name = column.name
            if not qualifier or not name:
                continue
            # Only unique columns are eligible; ambiguous ones (which may also
            # appear in COLUMN_TABLE_MAP, e.g. product_code) are never rewritten
            # unless exactly one candidate table is present in the query.
            if name in AMBIGUOUS_COLUMNS:
                continue
            correct_table = COLUMN_TABLE_MAP.get(name)
            if correct_table is None:
                continue

            current_table = query_alias_map.get(qualifier) or ALIAS_TABLE_MAP.get(qualifier)
            if current_table is None or current_table == correct_table:
                continue

            # Only fix if the correct table is actually in the query.
            if correct_table not in tables_present:
                continue

            new_qualifier = _qualifier_for(correct_table, query_alias_map)
            if new_qualifier is None:
                continue

            column.set("table", exp.to_identifier(new_qualifier))
            changed = True

        if not changed:
            return sql
        return tree.sql(dialect="mysql")
    except Exception:
        return sql


def _qualifier_for(table: str, query_alias_map: dict[str, str]) -> str | None:
    """The alias-or-name used for `table` in the query (prefer a real alias)."""
    match: str | None = None
    for qualifier, resolved in query_alias_map.items():
        if resolved == table:
            match = qualifier
            if qualifier != table:  # prefer an explicit alias over the bare name
                return qualifier
    return match


__all__ = ["fix_column_aliases"]
