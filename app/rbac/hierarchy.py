"""Sales-force hierarchy subtree resolution.

Ports the prototype's `get_hierarchy_subtree`: given the full sales-force
hierarchy (`sales_force_hier_value_t` rows) and a user's `sf_code`, resolve the
set of subordinate codes at each level *below* that user. The result feeds the
RBAC hierarchy predicate (see `app.rbac.injector`).

`resolve_subtree` is a pure function (fully unit-testable with an in-memory
fixture). `get_subtree_for` adds a short in-process TTL cache and a lazily
imported default fetcher — the `app.db.analytics` import lives INSIDE the
function body so importing this module never pulls in the analytics layer.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence

from app.rbac.profiles import RBACProfile

#: TTL for the in-process subtree cache (seconds).
_CACHE_TTL_S = 600

#: root sf_code -> (expiry_monotonic, resolved subtree)
_CACHE: dict[str, tuple[float, dict[int, set[str]]]] = {}


def _to_int_level(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def resolve_subtree(rows: Sequence[Mapping], root_sf_code: str) -> dict[int, set[str]]:
    """Resolve the subtree strictly *below* `root_sf_code`.

    `rows` are mappings with keys `sf_code`, `sf_name`, `sf_level_code`,
    `parent_code`. Returns `{level_code: {sf_code, ...}}` for every descendant,
    grouped by that descendant's own level. The root itself is not included
    (matching the prototype: a node's subtree is its reports, not itself).
    """
    root = str(root_sf_code).strip()

    children: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    for row in rows:
        parent = str(row.get("parent_code", "") or "").strip()
        if not parent:
            continue
        code = str(row.get("sf_code", "") or "").strip()
        level = _to_int_level(row.get("sf_level_code"))
        children[parent].append((code, level))

    result: dict[int, set[str]] = defaultdict(set)
    seen: set[str] = {root}
    queue: deque[str] = deque([root])
    while queue:
        current = queue.popleft()
        for code, level in children.get(current, ()):
            if not code or code in seen:
                continue
            seen.add(code)
            if level is not None:
                result[level].add(code)
            queue.append(code)

    return dict(result)


async def _default_fetch_rows() -> Sequence[Mapping]:
    """Fetch all hierarchy rows from MySQL (analytics imported lazily)."""
    from app.db.analytics import get_analytics

    analytics = get_analytics()
    query_result = await analytics.execute_readonly(
        "SELECT sf_code, sf_name, sf_level_code, parent_code FROM sales_force_hier_value_t"
    )
    return query_result.rows


async def get_subtree_for(
    profile: RBACProfile,
    fetch_rows: Callable[[], Awaitable[Sequence[Mapping]]] | None = None,
) -> dict[int, set[str]]:
    """Return the subtree below `profile.sf_code`, using a 10-minute TTL cache.

    Unrestricted profiles or profiles without an `sf_code` have no subtree.
    `fetch_rows` is injectable for testing; the default fetches from MySQL.
    """
    if profile.is_unrestricted or not profile.sf_code:
        return {}

    root = str(profile.sf_code).strip()
    now = time.monotonic()

    cached = _CACHE.get(root)
    if cached is not None and cached[0] > now:
        return cached[1]

    fetcher = fetch_rows or _default_fetch_rows
    rows = await fetcher()
    subtree = resolve_subtree(rows, root)

    _CACHE[root] = (now + _CACHE_TTL_S, subtree)
    return subtree


def clear_cache() -> None:
    """Clear the in-process subtree cache (test helper)."""
    _CACHE.clear()


__all__ = ["resolve_subtree", "get_subtree_for", "clear_cache"]
