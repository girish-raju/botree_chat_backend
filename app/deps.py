"""FastAPI dependencies: DB session and current-user resolution.

`get_session` yields a per-request `AsyncSession`, committing on success and
rolling back on error. `get_current_user` decodes the bearer JWT and loads
the corresponding active `User` row, raising `AuthError` otherwise.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import decode_token
from app.config import Settings, get_settings
from app.db.models import User
from app.db.postgres import get_sessionmaker
from app.errors import AuthError

logger = structlog.get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


async def _safe_rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.warning("session_rollback_failed", exc_info=True)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an `AsyncSession`, committing on success and rolling back on error.

    Streaming endpoints hold this session open for the life of the response, so
    a client disconnect (``CancelledError``, a ``BaseException``) or a DB op
    that failed mid-stream can leave the session unable to commit. We roll back
    on any exception, and if the final commit itself fails (e.g. a poisoned
    session after the response was already streamed) we roll back and log
    rather than surfacing an ASGI error for a request the client already left.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
        except BaseException:
            await _safe_rollback(session)
            raise
        else:
            try:
                await session.commit()
            except Exception as exc:
                # Expected when a streaming client disconnects mid-response and
                # poisons the session; we've already rolled back. Log the cause
                # without a full traceback to avoid alarming noise.
                await _safe_rollback(session)
                logger.warning("session_commit_failed", error=str(exc))


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Resolve the current authenticated, active `User` from the bearer token."""
    if credentials is None:
        raise AuthError("Invalid or expired token")

    payload = decode_token(credentials.credentials, settings)
    subject = payload.get("sub")
    if not subject:
        raise AuthError("Invalid or expired token")

    try:
        user_id = uuid.UUID(str(subject))
    except ValueError as exc:
        raise AuthError("Invalid or expired token") from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("Invalid or expired token")

    return user


__all__ = ["get_session", "get_current_user"]
