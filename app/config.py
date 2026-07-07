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

    # Server bind address for `python -m app.main`. Defaults to 0.0.0.0:8888 so
    # a reverse proxy / load balancer on the same host can reach it. Override
    # per environment via HOST / PORT (e.g. `PORT=8000` for local dev alongside
    # the frontend). The Docker image's CMD also honors $PORT.
    host: str = "0.0.0.0"
    port: int = 8888

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
    # Embeddings run via the Cloudflare API (same BGE model, no local download).
    cloudflare_embedding_model: str = "@cf/baai/bge-small-en-v1.5"
    # Speech-to-text for voice input (POST /api/transcribe).
    cloudflare_whisper_model: str = "@cf/openai/whisper-large-v3-turbo"

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
    # For managed hosts (Railway/Render) that only support env vars: the SSH
    # private key base64-encoded. Written to ssh_key_path on startup if set.
    ssh_key_b64: str = ""
    # Seconds to wait for a TCP connection to the SSH bastion before giving up.
    # Kept short so an unreachable/firewalled bastion fails fast with a clear
    # error instead of hanging on the OS default (~45-90s) and stalling the
    # request until the client disconnects.
    ssh_connect_timeout_s: int = 8

    # Cache
    semantic_threshold: float = 0.92
    result_cache_ttl_s: int = 300
    result_cache_sweep_interval_s: int = 300
    sql_row_cap: int = 50

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
