"""In-process rate limiting middleware.

A fixed-window limiter keyed by authenticated user id (best-effort JWT
decode) or client IP, with stricter per-minute limits for the hot endpoints
(`POST /api/chat`, `POST /api/auth/login`) and a default limit everywhere
else. `/healthz` and `/readyz` are always exempt.

This is an in-process, single-instance limiter: buckets live in a plain
module-level dict, so it does NOT coordinate across multiple worker
processes or replicas. If this service is ever scaled horizontally behind a
load balancer, the limit becomes "N times the intended rate" (one bucket set
per instance) -- at that point this needs to move to a shared store (e.g.
Redis `INCR`/`EXPIRE` or a token-bucket Lua script) to remain meaningful.

Settings are read live via `get_settings()` on every request (not cached at
import time) so tests can flip `rate_limit_enabled` / lower the limits on the
fly. Call `reset()` to clear all bucket state between tests.
"""

from __future__ import annotations

import json
import time

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.jwt import decode_token
from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)

#: Paths that are never rate limited (liveness/readiness probes).
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})

#: Fixed window size for all buckets -- limits are expressed "per minute".
_WINDOW_S = 60.0

#: Buckets idle longer than this are dropped by the opportunistic prune, so
#: memory doesn't grow unboundedly with the number of distinct users/IPs seen.
_IDLE_TTL_S = 10 * 60.0

#: Minimum spacing between prune sweeps (pruning walks the whole dict).
_PRUNE_INTERVAL_S = 60.0


class _Bucket:
    __slots__ = ("window_start", "count", "last_seen")

    def __init__(self, window_start: float, count: int, last_seen: float) -> None:
        self.window_start = window_start
        self.count = count
        self.last_seen = last_seen


_buckets: dict[str, _Bucket] = {}
_last_prune = 0.0


def reset() -> None:
    """Clear all bucket state. Intended for test isolation."""
    global _last_prune
    _buckets.clear()
    _last_prune = 0.0


def _prune(now: float) -> None:
    global _last_prune
    if now - _last_prune < _PRUNE_INTERVAL_S:
        return
    _last_prune = now
    stale_keys = [key for key, bucket in _buckets.items() if now - bucket.last_seen > _IDLE_TTL_S]
    for key in stale_keys:
        _buckets.pop(key, None)


def _check(key: str, limit: int, now: float) -> tuple[bool, int]:
    """Return `(allowed, retry_after_s)` for `key` under a fixed-window limit.

    A non-positive `limit` always rejects (with a 1s retry-after) -- treated
    as "blocked", not "unlimited".
    """
    _prune(now)
    bucket = _buckets.get(key)

    if bucket is None or now - bucket.window_start >= _WINDOW_S:
        _buckets[key] = _Bucket(window_start=now, count=1, last_seen=now)
        return (limit > 0), 0

    bucket.last_seen = now
    if bucket.count >= limit:
        retry_after = max(1, int(bucket.window_start + _WINDOW_S - now + 0.999))
        return False, retry_after

    bucket.count += 1
    return True, 0


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value
    return None


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"


def _bucket_identity(scope: Scope, settings: Settings) -> str:
    """Best-effort caller identity: authenticated user id, else client IP.

    JWT decoding failures (missing/expired/invalid token) are swallowed --
    this is only used for bucketing, not authorization, so it must never
    hard-fail the request. An absent or bad token simply falls back to IP.
    """
    auth = _header(scope, b"authorization")
    if auth:
        try:
            scheme, _, token = auth.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token:
                payload = decode_token(token, settings)
                subject = payload.get("sub")
                if subject:
                    return f"user:{subject}"
        except Exception:  # noqa: BLE001 - best-effort identity only
            pass
    return f"ip:{_client_ip(scope)}"


def _route_bucket(path: str, method: str) -> str:
    """Which per-route limit applies: "chat", "login", or "default"."""
    if method == "POST" and path == "/api/chat":
        return "chat"
    if method == "POST" and path == "/api/auth/login":
        return "login"
    return "default"


def _limit_for(route_bucket: str, settings: Settings) -> int:
    if route_bucket == "chat":
        return settings.rate_limit_chat_per_min
    if route_bucket == "login":
        return settings.rate_limit_login_per_min
    return settings.rate_limit_default_per_min


def _request_id_from_scope(scope: Scope) -> str:
    state = scope.get("state")
    if isinstance(state, dict):
        request_id = state.get("request_id")
        if request_id:
            return str(request_id)
    header = _header(scope, b"x-request-id")
    return header.decode("latin-1") if header else ""


class RateLimitMiddleware:
    """ASGI middleware enforcing per-identity, per-route request rate limits."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        route_bucket = _route_bucket(path, method)
        limit = _limit_for(route_bucket, settings)
        identity = _bucket_identity(scope, settings)
        key = f"{route_bucket}:{identity}"

        allowed, retry_after = _check(key, limit, time.monotonic())
        if not allowed:
            logger.warning("rate_limited", key=key, path=path, retry_after=retry_after)
            await self._reject(scope, send, retry_after)
            return

        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, send: Send, retry_after: int) -> None:
        request_id = _request_id_from_scope(scope)
        body = json.dumps(
            {
                "error": {
                    "code": "rate_limited",
                    "message": "Rate limit exceeded. Please slow down and try again.",
                    "request_id": request_id,
                }
            }
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"retry-after", str(retry_after).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = ["RateLimitMiddleware", "reset"]
