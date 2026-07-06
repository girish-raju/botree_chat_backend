"""Tests for the health/readiness endpoints."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_with_no_probes(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {}


async def test_error_envelope_carries_request_id(client: AsyncClient) -> None:
    """Error responses must include the per-request id (echoed in the header too)."""
    response = await client.get("/api/auth/me")  # no token → 401 AuthError
    assert response.status_code == 401
    request_id = response.json()["error"]["request_id"]
    assert request_id
    assert response.headers["x-request-id"] == request_id
