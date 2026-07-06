"""Seed the six RBAC demo users, ported from `MOCK_USER_CONFIG` in
`conversational_bot_v15.py` (lines ~304-352).

Usernames are the lowercased role (vp, zsm, rsm, bm, asm, so); sf_code,
sf_level, and geo scope are carried over unchanged from the original mock
config. Password for all seeded users comes from the `SEED_PASSWORD` env var
(default: "botree123"), bcrypt-hashed.

Idempotent: re-running upserts by username rather than duplicating rows.

Usage:
    python scripts/seed_users.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.models import User

# Ported from MOCK_USER_CONFIG (conversational_bot_v15.py, ~lines 304-352).
SEED_PROFILES: list[dict[str, Any]] = [
    {
        "username": "vp",
        "display_name": "Rahul - VP Sales",
        "role": "VP",
        "sf_code": "100",
        "sf_level": 100,
        "allowed_geo_col": None,
        "allowed_geo_vals": [],
    },
    {
        "username": "zsm",
        "display_name": "Amit - ZSM South",
        "role": "ZSM",
        "sf_code": "202",
        "sf_level": 200,
        "allowed_geo_col": "geo_hier2_name",
        "allowed_geo_vals": ["SOUTH"],
    },
    {
        "username": "rsm",
        "display_name": "Priya - RSM South",
        "role": "RSM",
        "sf_code": "303",
        "sf_level": 300,
        "allowed_geo_col": "geo_hier3_name",
        "allowed_geo_vals": ["REGION 6"],
    },
    {
        "username": "bm",
        "display_name": "Suresh - BM Chennai",
        "role": "BM",
        "sf_code": "414",
        "sf_level": 400,
        "allowed_geo_col": "geo_hier4_name",
        "allowed_geo_vals": ["TAMILNADU STATE"],
    },
    {
        "username": "asm",
        "display_name": "Aasim - ASM Chennai",
        "role": "ASM",
        "sf_code": "10085",
        "sf_level": 500,
        "allowed_geo_col": "geo_hier6_name",
        "allowed_geo_vals": ["CHENNAI District"],
    },
    {
        "username": "so",
        "display_name": "Rakesh - SO Chennai",
        "role": "SO",
        "sf_code": None,
        "sf_level": 600,
        "allowed_geo_col": "geo_hier7_name",
        "allowed_geo_vals": ["Trichy Town"],
    },
]


async def seed_users() -> list[str]:
    """Upsert the seed users by username. Returns the list of usernames seeded."""
    settings = get_settings()
    password = os.environ.get("SEED_PASSWORD", "botree123")
    password_hash = hash_password(password)

    engine = create_async_engine(settings.pg_dsn)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)

    seeded: list[str] = []
    try:
        async with sessionmaker() as session:
            for profile in SEED_PROFILES:
                result = await session.execute(
                    select(User).where(User.username == profile["username"])
                )
                user = result.scalar_one_or_none()

                if user is None:
                    user = User(username=profile["username"], password_hash=password_hash)
                    session.add(user)
                else:
                    user.password_hash = password_hash

                user.display_name = profile["display_name"]
                user.role = profile["role"]
                user.sf_code = profile["sf_code"]
                user.sf_level = profile["sf_level"]
                user.allowed_geo_col = profile["allowed_geo_col"]
                user.allowed_geo_vals = profile["allowed_geo_vals"]
                user.is_active = True

                seeded.append(profile["username"])

            await session.commit()
    finally:
        await engine.dispose()

    return seeded


async def main() -> None:
    seeded = await seed_users()
    print(f"Seeded {len(seeded)} users: {', '.join(seeded)}")


if __name__ == "__main__":
    asyncio.run(main())
