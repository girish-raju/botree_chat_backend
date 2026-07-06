"""Selects and caches the configured `LLMProvider` implementation."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.cloudflare_provider import CloudflareProvider

_VALID_PROVIDERS = ("anthropic", "cloudflare")

_provider: LLMProvider | None = None


def _build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_provider == "cloudflare":
        return CloudflareProvider(settings)
    raise ValueError(
        f"Unknown llm_provider {settings.llm_provider!r}; valid options are: "
        f"{', '.join(_VALID_PROVIDERS)}"
    )


def get_provider(settings: Settings | None = None) -> LLMProvider:
    """Return the process-wide singleton `LLMProvider` for the configured backend.

    Built once and cached; call `reset_provider()` (e.g. between tests) to
    force a rebuild on the next call.
    """
    global _provider
    if _provider is None:
        _provider = _build_provider(settings or get_settings())
    return _provider


def reset_provider() -> None:
    """Clear the cached provider singleton. Intended for use in tests."""
    global _provider
    _provider = None


__all__ = ["get_provider", "reset_provider"]
