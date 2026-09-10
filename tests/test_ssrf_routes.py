"""SSRF guard route wiring — private targets never reach the browser.

For each fetch route (``/v1/fetch``, ``/v1/extract-price``,
``/v1/fetch-image``), a private/metadata target returns the route's existing
``{ok:false, reason:"fetch_failed"}`` body — the same contract as a transport
failure (error-handling.md) — and the browser manager's ``ensure_browser`` /
``begin_fetch`` are never called: the guard runs before any of that.

Offline: the guard fires in the route handler before ``begin_fetch``, so no
browser/page fake is needed — a no-op manager that asserts (via counters) it
was never touched is enough.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus.config import Settings
from argus.diagnostics import FailureTracker
from argus.routes.extract import router as extract_router
from argus.routes.fetch import router as fetch_router
from argus.routes.fetch_image import router as fetch_image_router

AUTH = {"Authorization": "Bearer secret"}
BLOCKED_URL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


class _NoBrowserManager:
    """A manager that records entry into browser-touching methods.

    For a blocked-SSRF URL the guard returns before ``begin_fetch``, so neither
    ``begin_fetch`` nor ``ensure_browser`` may run. Counters prove it.
    """

    def __init__(self) -> None:
        self.begin_calls = 0
        self.ensure_calls = 0

    def begin_fetch(self) -> None:
        self.begin_calls += 1

    def end_fetch(self) -> None:
        pass

    async def ensure_browser(self) -> None:
        self.ensure_calls += 1


def _make_app(router) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings.model_construct(
        api_tokens="secret", auth_disabled=False, fetch_allow_private=False
    )
    app.state.browser_manager = _NoBrowserManager()
    app.state.failure_tracker = FailureTracker()
    app.include_router(router)
    return app


def _manager(app: FastAPI) -> _NoBrowserManager:
    return app.state.browser_manager  # type: ignore[no-any-return]


# --- /v1/fetch ------------------------------------------------------------------

def test_fetch_ssrf_block_returns_fetch_failed_without_browser() -> None:
    app = _make_app(fetch_router)
    client = TestClient(app)
    response = client.post("/v1/fetch", json={"url": BLOCKED_URL}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "fetch_failed",
        "signature": None,
        "retryable": None,
    }
    m = _manager(app)
    assert m.begin_calls == 0
    assert m.ensure_calls == 0


# --- /v1/extract-price ---------------------------------------------------------

def test_extract_ssrf_block_returns_fetch_failed_without_browser() -> None:
    app = _make_app(extract_router)
    client = TestClient(app)
    response = client.post(
        "/v1/extract-price", json={"url": BLOCKED_URL, "renderWaitMs": 0}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "fetch_failed",
        "signature": None,
        "retryable": None,
    }
    m = _manager(app)
    assert m.begin_calls == 0
    assert m.ensure_calls == 0


# --- /v1/fetch-image -----------------------------------------------------------

def test_fetch_image_ssrf_block_returns_fetch_failed_without_browser() -> None:
    app = _make_app(fetch_image_router)
    client = TestClient(app)
    response = client.post(
        "/v1/fetch-image", json={"url": BLOCKED_URL}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "fetch_failed"}
    m = _manager(app)
    assert m.begin_calls == 0
    assert m.ensure_calls == 0
