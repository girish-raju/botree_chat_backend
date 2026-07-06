"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.health import readiness_probes
from app.api.health import router as health_router
from app.api.threads import router as threads_router
from app.cache.embeddings import get_embedder, init_embedder
from app.config import get_settings
from app.db.analytics import dispose_analytics, get_analytics, init_analytics
from app.db.postgres import dispose_engine, init_engine, postgres_ready
from app.errors import register_exception_handlers
from app.logging import RequestContextMiddleware, configure_logging
from app.middleware.ratelimit import RateLimitMiddleware
from app.tasks.sweeper import start_sweeper, stop_sweeper

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Initializes the Postgres connection pool on startup and registers its
    readiness probe. Later phases will add the MySQL/SSH tunnel manager and
    the sentence-transformers embedder here, tearing them down on shutdown.
    """
    settings = get_settings()
    init_engine(settings)
    readiness_probes["postgres"] = postgres_ready

    init_analytics(settings)
    readiness_probes["mysql"] = get_analytics().ready

    init_embedder(settings)
    asyncio.create_task(get_embedder().warmup())

    start_sweeper(app, settings)

    logger.info("app_startup")
    yield
    logger.info("app_shutdown")
    await stop_sweeper(app)
    await dispose_engine()
    dispose_analytics()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(title="Botree Chat Backend", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Runs inside RequestContextMiddleware (so 429s carry a request id) but
    # outside CORS/the router (so limited requests never reach real work).
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(threads_router)
    app.include_router(chat_router)

    return app


app = create_app()
