"""Application configuration.

Settings are loaded from environment variables (and an optional `.env` file)
via pydantic-settings. Field names here are a contract relied upon by later
phases (database session setup, LLM clients, MySQL analytics connector,
SSH tunnel manager, semantic cache, etc.) — do not rename without checking
downstream usage.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized application settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "dev"
    log_level: str = "INFO"

    # Auth
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 720

    # Postgres
    pg_dsn: str = "postgresql+asyncpg://botree:botree@localhost:5432/botree_chat"

    # LLM
    llm_provider: str = "anthropic"  # anthropic | cloudflare

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model_sql: str = "claude-sonnet-5"
    anthropic_model_small: str = "claude-haiku-4-5"

    # Cloudflare
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    cloudflare_model: str = "@cf/meta/llama-3.1-8b-instruct"

    # MySQL analytics
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""
    mysql_query_timeout_s: int = 15

    # SSH tunnel
    ssh_tunnel_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_path: str = ""
    ssh_key_password: str = ""

    # Cache
    semantic_threshold: float = 0.92
    result_cache_ttl_s: int = 300
    result_cache_sweep_interval_s: int = 300
    sql_row_cap: int = 50
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # SQL safety
    require_sql_approval: bool = False

    # Rate limiting (in-process, single-instance; see app/middleware/ratelimit.py)
    rate_limit_enabled: bool = True
    rate_limit_chat_per_min: int = 20
    rate_limit_login_per_min: int = 10
    rate_limit_default_per_min: int = 120


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()
