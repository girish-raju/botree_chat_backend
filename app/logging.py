"""Structured logging configuration.

Uses structlog for both dev (console) and prod (JSON) rendering, plus a
contextvar-based request id that is bound per-request and included in every
log line emitted during that request's lifetime.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings

_REQUEST_ID_KEY = "request_id"


def bind_request_id(request_id: str) -> None:
    """Bind a request id into the structlog contextvars for the current context."""
    structlog.contextvars.bind_contextvars(**{_REQUEST_ID_KEY: request_id})


def configure_logging(settings: Settings) -> None:
    """Configure structlog (and stdlib logging) based on application settings."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.app_env == "dev":
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware:
    """ASGI middleware that binds a per-request id and echoes it as a response header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        bind_request_id(request_id)
        # Expose the id on request.state so exception handlers can echo it into
        # the error envelope (Starlette's request.state reads from scope["state"]).
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()


def request_context_middleware(app: ASGIApp) -> ASGIApp:
    """Factory returning the ASGI request-context middleware wrapping `app`."""
    return RequestContextMiddleware(app)


__all__ = [
    "bind_request_id",
    "configure_logging",
    "RequestContextMiddleware",
    "request_context_middleware",
]
