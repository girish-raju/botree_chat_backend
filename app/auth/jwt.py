"""JWT creation/decoding via PyJWT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import Settings
from app.errors import AuthError


def create_token(user_id: str, username: str, role: str, settings: Settings) -> str:
    """Create a signed HS256 JWT encoding the user's id, username, and role."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """Decode and validate a JWT, raising `AuthError` if expired or invalid."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token") from exc


__all__ = ["create_token", "decode_token"]
