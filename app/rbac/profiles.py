"""RBAC profiles derived from the authenticated user.

An `RBACProfile` is the immutable, hashable scope descriptor threaded through
SQL scoping and result-cache keying. `rbac_fingerprint` produces a stable hash
of the scope so identical SQL run under two different scopes never share a
cached result (see result-cache keying in the pipeline).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import User


@dataclass(frozen=True)
class RBACProfile:
    """Immutable row-visibility scope for a single user."""

    user_id: str
    role: str
    sf_level: int | None
    sf_code: str | None
    geo_col: str | None
    geo_vals: tuple[str, ...]

    @property
    def is_unrestricted(self) -> bool:
        """True for top-of-hierarchy users who see everything (no scoping)."""
        return self.sf_level == 100 or self.role == "VP"


def profile_from_user(user: User) -> RBACProfile:
    """Build an `RBACProfile` from a `User` ORM row."""
    geo_vals_raw = user.allowed_geo_vals or []
    geo_vals = tuple(str(v) for v in geo_vals_raw)
    return RBACProfile(
        user_id=str(user.id),
        role=user.role,
        sf_level=user.sf_level,
        sf_code=user.sf_code,
        geo_col=user.allowed_geo_col,
        geo_vals=geo_vals,
    )


def rbac_fingerprint(profile: RBACProfile) -> str:
    """Return a stable sha256 hex digest of the profile's *scope*.

    Only scope-defining fields are included (not `user_id`): two distinct users
    with an identical scope share cached results safely, while any difference in
    role / level / code / geo produces a different fingerprint. Stable across
    processes (no salt, canonical ordering of geo values).
    """
    canonical = "|".join(
        [
            profile.role or "",
            "" if profile.sf_level is None else str(profile.sf_level),
            profile.sf_code or "",
            profile.geo_col or "",
            ",".join(sorted(profile.geo_vals)),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["RBACProfile", "profile_from_user", "rbac_fingerprint"]
