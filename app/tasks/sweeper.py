"""Background TTL sweeper for the result cache.

Every `settings.result_cache_sweep_interval_s` seconds, opens a session and
calls `ResultCache.sweep()` to delete expired `result_cache` rows. This is
purely a space-reclamation job -- `ResultCache.get()` already treats expired
rows as misses and lazily deletes them on read, so correctness never depends
on the sweeper running; it just keeps the table from growing unboundedly
between reads.

`start_sweeper` / `stop_sweeper` are called from the FastAPI lifespan (see
`app/main.py`), stashing the task on `app.state` so it can be cancelled
cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.results import ResultCache
from app.config import Settings
from app.db.postgres import get_sessionmaker

logger = structlog.get_logger(__name__)

_TASK_ATTR = "result_cache_sweeper_task"


async def sweep_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    cache: ResultCache | None = None,
) -> int:
    """Run a single sweep pass against `sessionmaker`.

    Never raises: any exception (DB error, etc.) is logged and swallowed,
    returning 0, so a bad sweep never crashes the calling loop.
    """
    cache = cache if cache is not None else ResultCache()
    try:
        async with sessionmaker() as session:
            deleted = await cache.sweep(session)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("result_cache_sweep_failed")
        return 0

    if deleted:
        logger.info("result_cache_swept", deleted=deleted)
    return deleted


async def _sweep_loop(settings: Settings) -> None:
    interval = max(1, settings.result_cache_sweep_interval_s)
    cache = ResultCache()
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                sessionmaker = get_sessionmaker()
            except Exception:
                logger.exception("result_cache_sweep_failed")
                continue
            await sweep_once(sessionmaker, cache)
    except asyncio.CancelledError:
        logger.info("result_cache_sweeper_cancelled")
        raise


def start_sweeper(app: FastAPI, settings: Settings) -> None:
    """Start the background sweep loop, stashing the task on `app.state`."""
    task = asyncio.create_task(_sweep_loop(settings))
    setattr(app.state, _TASK_ATTR, task)


async def stop_sweeper(app: FastAPI) -> None:
    """Cancel the background sweep loop and await its clean shutdown."""
    task: Any = getattr(app.state, _TASK_ATTR, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    setattr(app.state, _TASK_ATTR, None)


__all__ = ["sweep_once", "start_sweeper", "stop_sweeper"]
