"""Health and readiness endpoints.

`readiness_probes` is a small registry that later phases populate (e.g. a
"postgres" probe that pings the connection pool, a "mysql" probe that checks
the analytics connector / SSH tunnel). `/readyz` runs every registered probe
and reports pass/fail per check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

#: Registry of readiness probes: name -> async callable returning True if healthy.
readiness_probes: dict[str, Callable[[], Awaitable[bool]]] = {}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe: always returns ok if the process is serving requests."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe: runs all registered probes and reports overall status."""
    checks: dict[str, bool] = {}
    for name, probe in readiness_probes.items():
        try:
            checks[name] = await probe()
        except Exception:
            logger.exception("readiness_probe_failed", probe=name)
            checks[name] = False

    all_ready = all(checks.values())
    status_code = 200 if all_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if all_ready else "not_ready",
            "checks": checks,
        },
    )


__all__ = ["router", "readiness_probes"]
