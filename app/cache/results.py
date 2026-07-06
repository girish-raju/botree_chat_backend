"""L2 result cache: caches the final row set for a fully-resolved
(RBAC-scoped, LIMIT-enforced) SQL statement, keyed by that SQL plus the
requesting user's RBAC fingerprint.

Two users (or the same user across sessions) asking a question that resolves
to byte-identical scoped SQL get served from `result_cache` instead of
re-hitting MySQL, for `settings.result_cache_ttl_s` seconds. This is
deliberately keyed on the *final* SQL (post-RBAC-injection), not the
question, so it can never leak one user's scoped rows to another user whose
scope differs — a different fingerprint always produces a different key.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResultCacheEntry


def result_cache_key(final_sql: str, rbac_fingerprint: str) -> str:
    """Stable cache key for a (final SQL, RBAC scope) pair.

    Two calls with the same SQL but different fingerprints (or vice versa)
    always produce different keys.
    """
    digest = hashlib.sha256()
    digest.update(final_sql.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(rbac_fingerprint.encode("utf-8"))
    return digest.hexdigest()


def jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MySQL driver value types into JSON-serializable equivalents.

    `Decimal` -> `float`, `date`/`datetime` -> ISO-8601 string, `bytes` ->
    `str` (decoded as utf-8, falling back to `repr` if not valid utf-8).
    """
    return [{k: _jsonable_value(v) for k, v in row.items()} for row in rows]


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    return value


class ResultCache:
    """Get/put/sweep against `result_cache`."""

    async def get(self, session: AsyncSession, key: str) -> ResultCacheEntry | None:
        """Return the entry for `key` if present and not expired.

        An expired entry is treated as a miss and deleted (lazy cleanup);
        callers don't need to run `sweep` for correctness, only to reclaim
        space proactively.
        """
        entry = await session.get(ResultCacheEntry, key)
        if entry is None:
            return None

        if _as_aware(entry.expires_at) <= datetime.now(timezone.utc):
            await session.delete(entry)
            await session.flush()
            return None

        return entry

    async def put(
        self,
        session: AsyncSession,
        key: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        row_count: int,
        ttl_s: int,
    ) -> None:
        """Upsert the result set for `key`, expiring `ttl_s` seconds from now."""
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
        safe_rows = jsonable_rows(rows)

        entry = await session.get(ResultCacheEntry, key)
        if entry is not None:
            entry.columns = columns
            entry.rows = safe_rows
            entry.row_count = row_count
            entry.expires_at = expires_at
        else:
            session.add(
                ResultCacheEntry(
                    cache_key=key,
                    columns=columns,
                    rows=safe_rows,
                    row_count=row_count,
                    expires_at=expires_at,
                )
            )
        await session.flush()

    async def sweep(self, session: AsyncSession) -> int:
        """Delete all expired rows. Returns the number of rows deleted."""
        now = datetime.now(timezone.utc)
        result = await session.execute(delete(ResultCacheEntry).where(ResultCacheEntry.expires_at <= now))
        return result.rowcount or 0


def _as_aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat naive datetimes as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = ["result_cache_key", "jsonable_rows", "ResultCache"]
