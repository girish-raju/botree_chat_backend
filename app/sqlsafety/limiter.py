"""Outer-LIMIT enforcement.

`enforce_limit` guarantees the OUTERMOST query never returns more than `cap`
rows. It only touches the top-level statement's LIMIT: LIMIT clauses inside
subqueries or CTEs are left untouched (they are semantically meaningful and
capping them would change results). For a UNION, the outer LIMIT applies to
the whole union, which is exactly the node `enforce_limit` receives.
"""

from __future__ import annotations

from sqlglot import exp


def enforce_limit(tree: exp.Expression, cap: int = 50) -> exp.Expression:
    """Clamp/add the outermost LIMIT of `tree` to at most `cap`.

    * no outer LIMIT  -> add `LIMIT cap`
    * outer LIMIT > cap -> clamp to `cap`
    * outer LIMIT <= cap -> leave unchanged

    Mutates and returns `tree`.
    """
    limit_node = tree.args.get("limit")

    if limit_node is None:
        tree.limit(cap, copy=False)
        return tree

    current = _limit_value(limit_node)
    if current is None or current > cap:
        limit_node.set("expression", exp.Literal.number(cap))
    return tree


def _limit_value(limit_node: exp.Expression) -> int | None:
    """Extract the integer count from a LIMIT node, or None if not a plain int."""
    expr = getattr(limit_node, "expression", None)
    if expr is None:
        return None
    try:
        return int(expr.name)
    except (ValueError, TypeError, AttributeError):
        return None


def tree_to_sql(tree: exp.Expression) -> str:
    """Render `tree` back to a MySQL SQL string."""
    return tree.sql(dialect="mysql")


__all__ = ["enforce_limit", "tree_to_sql"]
