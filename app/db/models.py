"""ORM models for the application (Postgres) database.

Types are chosen so the schema can also be created on SQLite for tests:
- JSON columns use `sa.JSON().with_variant(JSONB, "postgresql")`.
- UUID columns use `sa.Uuid` (SQLAlchemy's cross-dialect UUID type).
- `SqlAuditLog.id` uses `BigInteger` on Postgres but falls back to `Integer`
  on SQLite (SQLite has no native autoincrement bigint).

`QueryCacheEntry.question_embedding` uses `pgvector.sqlalchemy.Vector`, which
does not compile on SQLite. Tests must exclude the `query_cache` table when
creating tables against a SQLite engine (see `tests/conftest.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _json_type() -> Any:
    """A JSON column type that uses JSONB on Postgres, plain JSON elsewhere."""
    return sa.JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    display_name: Mapped[str] = mapped_column(sa.String(255))
    role: Mapped[str] = mapped_column(sa.String(32))  # VP|ZSM|RSM|BM|ASM|SO
    sf_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    sf_level: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    allowed_geo_col: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    allowed_geo_vals: Mapped[list[str] | None] = mapped_column(_json_type(), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        Index("ix_threads_user_status_updated", "user_id", "status", "updated_at"),
    )

    # This IS the frontend remoteId.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, ForeignKey("users.id"))
    title: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="regular", server_default="regular")
    head_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"

    # Frontend-generated id.
    id: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, ForeignKey("threads.id"), index=True
    )
    parent_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    format: Mapped[str] = mapped_column(sa.String(32))
    content: Mapped[Any] = mapped_column(_json_type())  # opaque — backend never parses
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class QueryCacheEntry(Base):
    __tablename__ = "query_cache"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    normalized_question: Mapped[str] = mapped_column(sa.Text, unique=True, index=True)
    question_embedding: Mapped[Any] = mapped_column(Vector(384))
    sql_template: Mapped[str] = mapped_column(sa.Text)
    params_spec: Mapped[Any | None] = mapped_column(_json_type(), nullable=True)
    temporal_intent: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    hit_count: Mapped[int] = mapped_column(sa.Integer, default=0, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    is_valid: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ResultCacheEntry(Base):
    __tablename__ = "result_cache"

    cache_key: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    columns: Mapped[Any] = mapped_column(_json_type())
    rows: Mapped[Any] = mapped_column(_json_type())
    row_count: Mapped[int] = mapped_column(sa.Integer)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class SqlAuditLog(Base):
    __tablename__ = "sql_audit_log"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    question: Mapped[str] = mapped_column(sa.Text)
    rewritten_question: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    generated_sql: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    final_sql: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    cache_level: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)  # L0|L1|llm|result
    row_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32))  # ok|blocked|error
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


__all__ = [
    "Base",
    "User",
    "Thread",
    "Message",
    "QueryCacheEntry",
    "ResultCacheEntry",
    "SqlAuditLog",
]
