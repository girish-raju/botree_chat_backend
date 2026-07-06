"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("sf_code", sa.String(length=64), nullable=True),
        sa.Column("sf_level", sa.Integer(), nullable=True),
        sa.Column("allowed_geo_col", sa.String(length=128), nullable=True),
        sa.Column("allowed_geo_vals", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="regular", nullable=False),
        sa.Column("head_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_threads_user_status_updated",
        "threads",
        ["user_id", "status", sa.text("updated_at DESC")],
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("parent_id", sa.String(length=255), nullable=True),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("content", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])

    op.create_table(
        "query_cache",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("normalized_question", sa.Text(), nullable=False),
        sa.Column("question_embedding", Vector(384), nullable=False),
        sa.Column("sql_template", sa.Text(), nullable=False),
        sa.Column("params_spec", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("temporal_intent", sa.String(length=64), nullable=True),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_valid", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_query_cache_normalized_question", "query_cache", ["normalized_question"], unique=True
    )
    op.create_index(
        "ix_query_cache_embedding_hnsw",
        "query_cache",
        ["question_embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"question_embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "result_cache",
        sa.Column("cache_key", sa.String(length=255), primary_key=True),
        sa.Column("columns", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("rows", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sql_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rewritten_question", sa.Text(), nullable=True),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("final_sql", sa.Text(), nullable=True),
        sa.Column("cache_level", sa.String(length=32), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("sql_audit_log")
    op.drop_table("result_cache")
    op.drop_index("ix_query_cache_embedding_hnsw", table_name="query_cache")
    op.drop_index("ix_query_cache_normalized_question", table_name="query_cache")
    op.drop_table("query_cache")
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_threads_user_status_updated", table_name="threads")
    op.drop_table("threads")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
