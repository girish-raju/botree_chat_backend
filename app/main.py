"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import base64
import os
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
from app.cache.embeddings import dispose_embedder, get_embedder, init_embedder
from app.config import get_settings
from app.db.analytics import dispose_analytics, get_analytics, init_analytics
from app.db.postgres import dispose_engine, init_engine, postgres_ready
from app.errors import register_exception_handlers
from app.logging import RequestContextMiddleware, configure_logging
from app.middleware.ratelimit import RateLimitMiddleware
from app.tasks.sweeper import start_sweeper, stop_sweeper

logger = structlog.get_logger(__name__)


def _materialize_ssh_key(settings) -> None:
    """Write the SSH private key from a base64 env var to a file, if provided.

    Managed hosts (Railway/Render) only support env vars, not files, so the
    Bisk Farm SSH key is supplied as SSH_KEY_B64 and written to SSH_KEY_PATH
    here on startup. No-op when the key file already exists or B64 is unset.
    """
    if not (settings.ssh_key_b64 and settings.ssh_key_path):
        return
    if os.path.exists(settings.ssh_key_path):
        return
    try:
        data = base64.b64decode(settings.ssh_key_b64)
        parent = os.path.dirname(settings.ssh_key_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(settings.ssh_key_path, "wb") as fh:
            fh.write(data)
        os.chmod(settings.ssh_key_path, 0o600)
        logger.info("ssh_key_materialized", path=settings.ssh_key_path)
    except Exception:
        logger.warning("ssh_key_materialize_failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan hook.

    Initializes the Postgres connection pool and the MySQL/SSH tunnel manager
    on startup, registers their readiness probes, and warms up the Cloudflare-API
    embedder — tearing them all down on shutdown.
    """
    settings = get_settings()
    _materialize_ssh_key(settings)
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
    await dispose_embedder()


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


def main() -> None:
    """Run the app with uvicorn, binding Settings.host:Settings.port.

    Defaults to 0.0.0.0:8888 so a same-host reverse proxy (nginx / ALB) can
    reach it. Run with ``python -m app.main``. Override with env vars, e.g.
    ``PORT=8000 python -m app.main`` for local dev alongside the frontend.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
