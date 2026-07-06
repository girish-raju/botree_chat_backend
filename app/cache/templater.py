"""SQL templating: parameterize volatile literals so one cached template can
serve many differently-worded (but same-shaped) questions.

`parameterize_sql` walks a generated SQL statement and replaces date-like and
free-text string literals with named placeholders (`:date1`, `:str1`, ...),
recording their original values in a `params_spec` dict. The resulting
template is what gets stored in `QueryCacheEntry.sql_template` /
`params_spec` (see `app.cache.semantic.QueryCache.store`). `CURDATE()` /
`NOW()` / `CURRENT_DATE` are left untouched deliberately — they are *already*
relative (re-evaluate at execution time) and are the preferred form for date
filters, so nothing needs to be substituted for them at all.

`bind_template` is the inverse: given a template + its params_spec + the
*current* question, it rebuilds a concrete, executable SQL string. v1 is
deliberately conservative (see docstring below) and is designed to never
raise, since it sits on the cache-hit fast path.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_DIALECT = "mysql"

#: ISO-ish date/datetime literal values, e.g. '2026-07-06' or
#: '2026-07-06 00:00:00'. Deliberately conservative: only forms MySQL/the
#: generator actually emits are matched here.
_DATE_LITERAL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$"
)

#: Explicit calendar date appearing anywhere in free text (used to pull a
#: replacement date value out of a *new* question in `bind_template`).
_EXPLICIT_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_date_literal(value: str) -> bool:
    return bool(_DATE_LITERAL_RE.match(value.strip()))


def parameterize_sql(sql: str) -> tuple[str, dict[str, dict[str, Any]]]:
    """Replace date/string literals in `sql` with named placeholders.

    Returns `(template_sql, params_spec)` where `params_spec` maps each
    placeholder name (without the leading `:`) to `{"type": "date"|"str",
    "value": <original literal value>}`.

    Only string literals are touched: numeric literals (LIMIT counts,
    comparison thresholds like `> 1000`) are left exactly as they are, and so
    are relative-date function calls (`CURDATE()`, `NOW()`,
    `CURRENT_DATE`) since they aren't `Literal` nodes at all.

    If `sql` fails to parse, it is returned unchanged with an empty spec —
    callers should fall back to caching it verbatim (no templating).
    """
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except SqlglotError:
        return sql, {}

    params_spec: dict[str, dict[str, Any]] = {}
    date_n = 0
    str_n = 0

    for literal in list(tree.find_all(exp.Literal)):
        if not literal.is_string:
            continue

        value = literal.this
        if _is_date_literal(value):
            date_n += 1
            name = f"date{date_n}"
            params_spec[name] = {"type": "date", "value": value}
        else:
            str_n += 1
            name = f"str{str_n}"
            params_spec[name] = {"type": "str", "value": value}

        literal.replace(exp.Placeholder(this=name))

    template_sql = tree.sql(dialect=_DIALECT)
    return template_sql, params_spec


def _naive_bind(template_sql: str, params_spec: dict[str, dict[str, Any]]) -> str:
    """Regex-based fallback substitution, used only if AST round-tripping
    fails. Still escapes single quotes so it can never produce a string that
    breaks out of its literal context."""
    result = template_sql
    for name, spec in (params_spec or {}).items():
        value = str(spec.get("value", ""))
        escaped = value.replace("'", "''")
        result = result.replace(f":{name}", f"'{escaped}'")
    return result


def bind_template(template_sql: str, params_spec: dict[str, dict[str, Any]] | None, question: str) -> str:
    """Rebuild a concrete SQL string from `template_sql` for the given `question`.

    String parameters are always bound back to their originally-recorded
    value (v1 is conservative: it does not attempt to re-extract entity names
    from `question`). Date parameters are rebound from an explicit date found
    in `question` if one is present, else fall back to the originally
    recorded value (CURDATE()-relative SQL already handles date rollover on
    its own, so this only matters for genuinely explicit-date templates).

    Never raises: any failure falls back to the original recorded values via
    a regex-based substitution.
    """
    params_spec = params_spec or {}

    try:
        tree = sqlglot.parse_one(template_sql, read=_DIALECT)
    except SqlglotError:
        return _naive_bind(template_sql, params_spec)

    explicit_date_match = _EXPLICIT_DATE_RE.search(question or "")
    replacement_date = explicit_date_match.group(0) if explicit_date_match else None

    try:
        for placeholder in list(tree.find_all(exp.Placeholder)):
            name = placeholder.this
            spec = params_spec.get(name)
            if spec is None:
                continue

            value = spec.get("value")
            if spec.get("type") == "date" and replacement_date is not None:
                value = replacement_date

            placeholder.replace(exp.Literal.string(str(value)))

        return tree.sql(dialect=_DIALECT)
    except Exception:
        return _naive_bind(template_sql, params_spec)


__all__ = ["parameterize_sql", "bind_template"]
