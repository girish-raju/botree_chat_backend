"""Authentication endpoints: login + current-user profile."""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import create_token
from app.auth.passwords import verify_password
from app.config import Settings, get_settings
from app.db.models import User
from app.deps import get_current_user, get_session
from app.errors import AuthError

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UserSummary(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class LoginResponse(BaseModel):
    token: str
    user: UserSummary


class UserProfile(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    sf_code: str | None
    sf_level: int | None
    allowed_geo_col: str | None
    allowed_geo_vals: list[str] | None


@router.post("/login")
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    result = await session.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        raise AuthError("Invalid username or password")
    if not user.is_active:
        raise AuthError("User account is inactive")

    token = create_token(
        user_id=str(user.id), username=user.username, role=user.role, settings=settings
    )
    return LoginResponse(
        token=token,
        user=UserSummary(
            id=str(user.id),
            username=user.username,
            display_name=user.display_name,
            role=user.role,
        ),
    )


@router.get("/me")
async def me(user: Annotated[User, Depends(get_current_user)]) -> UserProfile:
    return UserProfile(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        sf_code=user.sf_code,
        sf_level=user.sf_level,
        allowed_geo_col=user.allowed_geo_col,
        allowed_geo_vals=user.allowed_geo_vals,
    )


__all__ = ["router"]
