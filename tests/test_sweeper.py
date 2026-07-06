"""Tests for `app.tasks.sweeper.sweep_once` (the TTL sweeper's core unit)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.results import ResultCache, result_cache_key
from app.tasks.sweeper import sweep_once


async def test_sweep_once_deletes_expired_and_keeps_fresh(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cache = ResultCache()
    expired_key = result_cache_key("SELECT 1", "fp")
    fresh_key = result_cache_key("SELECT 2", "fp")

    async with db_sessionmaker() as session:
        await cache.put(session, expired_key, ["a"], [{"a": 1}], row_count=1, ttl_s=-10)
        await cache.put(session, fresh_key, ["a"], [{"a": 1}], row_count=1, ttl_s=300)
        await session.commit()

    deleted = await sweep_once(db_sessionmaker, cache)
    assert deleted == 1

    async with db_sessionmaker() as session:
        assert await cache.get(session, fresh_key) is not None
        assert await cache.get(session, expired_key) is None


async def test_sweep_once_survives_injected_exception(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    class _ExplodingCache(ResultCache):
        async def sweep(self, session):  # noqa: D102 - test double
            raise RuntimeError("boom")

    # Must not raise -- errors are logged and swallowed, returning 0.
    deleted = await sweep_once(db_sessionmaker, _ExplodingCache())
    assert deleted == 0


async def test_sweep_once_survives_broken_sessionmaker() -> None:
    def _broken_sessionmaker():
        raise RuntimeError("no engine")

    deleted = await sweep_once(_broken_sessionmaker, ResultCache())
    assert deleted == 0
