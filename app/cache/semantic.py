"""L0 (exact) and L1 (semantic) query cache.

`QueryCache` looks up previously-answered questions in `query_cache` so a
repeated or paraphrased question can skip the LLM entirely:

  - `lookup_exact`: normalized-text equality (the cheap, always-tried-first path).
  - `lookup_semantic`: pgvector cosine-similarity nearest neighbor, gated by
    BOTH a similarity threshold AND an exact `temporal_intent` match. The
    temporal check is the critical false-positive guard: "sales today" and
    "sales yesterday" can be near-identical in embedding space but must never
    cache-hit against each other.

`query_cache` uses a `pgvector` column and only exists on Postgres (see
`app/db/models.py` / `tests/conftest.py`); this module is exercised in tests
via a mocked `AsyncSession`, not a real database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QueryCacheEntry


class QueryCache:
    """Exact + semantic lookups and writes against `query_cache`."""

    async def lookup_exact(
        self, session: AsyncSession, normalized_q: str
    ) -> QueryCacheEntry | None:
        """Return the valid entry whose `normalized_question` equals `normalized_q`.

        On hit, bumps `hit_count` / `last_hit_at` on the returned entry
        (caller is responsible for committing/flushing).
        """
        stmt = select(QueryCacheEntry).where(
            QueryCacheEntry.normalized_question == normalized_q,
            QueryCacheEntry.is_valid.is_(True),
        )
        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()
        if entry is None:
            return None

        self._bump_hit(entry)
        return entry

    async def lookup_semantic(
        self,
        session: AsyncSession,
        embedding: list[float],
        temporal_intent: str,
        threshold: float,
    ) -> tuple[QueryCacheEntry, float] | None:
        """Nearest-neighbor semantic lookup, gated by similarity + temporal match.

        Returns `(entry, similarity)` only when BOTH:
          - cosine similarity (`1 - cosine_distance`) >= `threshold`, AND
          - `entry.temporal_intent == temporal_intent`.

        Either condition failing is a miss (returns `None`), even if the
        other is a near-perfect match — this is what prevents "sales today"
        from matching a cached "sales yesterday" entry at similarity ~0.99.
        """
        dist_col = QueryCacheEntry.question_embedding.cosine_distance(embedding).label("dist")
        stmt = (
            select(QueryCacheEntry, dist_col)
            .where(QueryCacheEntry.is_valid.is_(True))
            .order_by(dist_col)
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            return None

        entry, dist = row
        similarity = 1.0 - dist

        if similarity < threshold:
            return None
        if entry.temporal_intent != temporal_intent:
            return None

        self._bump_hit(entry)
        return entry, similarity

    async def store(
        self,
        session: AsyncSession,
        *,
        normalized_q: str,
        embedding: list[float],
        sql_template: str,
        params_spec: dict[str, Any] | None,
        temporal_intent: str | None,
        created_by: uuid.UUID | None = None,
    ) -> None:
        """Upsert a `query_cache` row keyed by `normalized_question`.

        On conflict, refreshes the template/spec/embedding/temporal fields
        and resets `is_valid=True` (re-validates a previously invalidated
        entry, e.g. after a schema change was later reconciled).
        """
        stmt = pg_insert(QueryCacheEntry).values(
            id=uuid.uuid4(),
            normalized_question=normalized_q,
            question_embedding=embedding,
            sql_template=sql_template,
            params_spec=params_spec,
            temporal_intent=temporal_intent,
            created_by=created_by,
            is_valid=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[QueryCacheEntry.normalized_question],
            set_={
                "sql_template": stmt.excluded.sql_template,
                "params_spec": stmt.excluded.params_spec,
                "question_embedding": stmt.excluded.question_embedding,
                "temporal_intent": stmt.excluded.temporal_intent,
                "is_valid": True,
            },
        )
        await session.execute(stmt)

    async def add_alias(
        self,
        session: AsyncSession,
        normalized_q: str,
        source_entry: QueryCacheEntry,
    ) -> None:
        """Clone `source_entry`'s template/spec/temporal under a new phrasing.

        Inserts an L0 row for `normalized_q` (the new phrasing that produced
        a semantic hit against `source_entry`) so future exact-lookups of the
        same phrasing skip the semantic search entirely. Conflicts (the
        phrasing already has its own row) are ignored.
        """
        stmt = (
            pg_insert(QueryCacheEntry)
            .values(
                id=uuid.uuid4(),
                normalized_question=normalized_q,
                question_embedding=source_entry.question_embedding,
                sql_template=source_entry.sql_template,
                params_spec=source_entry.params_spec,
                temporal_intent=source_entry.temporal_intent,
                created_by=source_entry.created_by,
                is_valid=True,
            )
            .on_conflict_do_nothing(index_elements=[QueryCacheEntry.normalized_question])
        )
        await session.execute(stmt)

    async def invalidate_all(self, session: AsyncSession) -> int:
        """Mark every `query_cache` row invalid. Returns the number of rows affected."""
        stmt = update(QueryCacheEntry).where(QueryCacheEntry.is_valid.is_(True)).values(
            is_valid=False
        )
        result = await session.execute(stmt)
        return result.rowcount or 0

    @staticmethod
    def _bump_hit(entry: QueryCacheEntry) -> None:
        entry.hit_count = (entry.hit_count or 0) + 1
        entry.last_hit_at = datetime.now(timezone.utc)


__all__ = ["QueryCache"]
