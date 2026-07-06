"""Async Postgres engine + session factory.

`init_engine` / `dispose_engine` are called from the FastAPI lifespan (see
`app/main.py`). `get_sessionmaker` is used by `app/deps.py` to hand out
per-request `AsyncSession`s. `postgres_ready` is registered into
`app.api.health.readiness_probes` as the "postgres" check.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings

logger = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Create the module-level async engine + sessionmaker from `settings.pg_dsn`."""
    global _engine, _sessionmaker

    _engine = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(bind=_engine, expire_on_commit=False)
    logger.info("postgres_engine_initialized")
    return _engine


async def dispose_engine() -> None:
    """Dispose of the engine (closes the connection pool)."""
    global _engine, _sessionmaker

    if _engine is not None:
        await _engine.dispose()
        logger.info("postgres_engine_disposed")
    _engine = None
    _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the module-level sessionmaker, raising if the engine isn't initialized."""
    if _sessionmaker is None:
        raise RuntimeError("Postgres engine not initialized. Call init_engine() first.")
    return _sessionmaker


async def postgres_ready() -> bool:
    """Readiness probe: returns True if a trivial query succeeds."""
    if _sessionmaker is None:
        return False
    try:
        async with _sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("postgres_readiness_check_failed")
        return False


__all__ = [
    "init_engine",
    "dispose_engine",
    "get_sessionmaker",
    "postgres_ready",
]
