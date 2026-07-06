"""SQL safety core: parse-time guard, outer-limit enforcement, alias fixer.

The canonical pipeline order (see `app.rbac.injector.scoped_sql`) is:

    assert_safe(sql)  ->  apply_scope(tree, ...)  ->  enforce_limit(tree)

The fixer (`fix_column_aliases`) is a best-effort *pre*-guard step owned by the
generation/self-correction loop, not by this package's runtime path.
"""

from __future__ import annotations

from app.sqlsafety.fixer import fix_column_aliases
from app.sqlsafety.guard import assert_safe
from app.sqlsafety.limiter import enforce_limit, tree_to_sql

__all__ = [
    "assert_safe",
    "enforce_limit",
    "tree_to_sql",
    "fix_column_aliases",
]
