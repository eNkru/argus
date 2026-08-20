"""Health endpoint — unauthenticated readiness probe.

Uses a fake app.state.browser_manager (a SimpleNamespace) so the test does not
require the Camoufox browser to be importable/installed.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from argus.routes.health import router as health_router


def _make_app(browser_ready: bool) -> FastAPI:
    app = FastAPI()
    app.state.browser_manager = SimpleNamespace(is_ready=browser_ready)
    app.include_router(health_router)
    return app


def test_health_browser_absent() -> None:
    client = TestClient(_make_app(browser_ready=False))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "browser": "absent"}


def test_health_browser_ready() -> None:
    client = TestClient(_make_app(browser_ready=True))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "browser": "ready"}


def test_health_needs_no_auth() -> None:
    # No Authorization header present, yet 200 — health is open for readiness probes.
    client = TestClient(_make_app(browser_ready=False))
    response = client.get("/health")
    assert response.status_code == 200
