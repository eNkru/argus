"""``POST /v1/extract-price`` route — offline via faked browser lifecycle.

The route drives ``navigate.fetch_html``, which needs a browser/context/page
chain; these tests substitute SimpleNamespace-grade fakes so nothing imports
camoufox or touches a network. Assertions follow the house style: exact JSON
bodies (FastAPI keeps explicit-null fields in the response).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus.config import Settings
from argus.diagnostics import FailureTracker
from argus.routes.extract import router as extract_router


# --- fake Playwright chain -----------------------------------------------------


class _NullConcurrency:
    async def __aenter__(self) -> "_NullConcurrency":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakePage:
    def __init__(
        self,
        html: str,
        final_url: str = "https://example.test/pdp",
        status: int = 200,
        goto_exc: BaseException | None = None,
    ) -> None:
        self.html = html
        self.url = final_url
        self.status = status
        self.goto_exc = goto_exc

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: float | None = None
    ) -> object:
        if self.goto_exc is not None:
            raise self.goto_exc
        return SimpleNamespace(status=self.status)

    async def content(self) -> str:
        return self.html

    async def close(self) -> None:
        return None


class FakeContext:
    def __init__(self, page: FakePage, cookies_exc: BaseException | None = None) -> None:
        self.page = page
        self.cookies_exc = cookies_exc

    async def new_page(self) -> FakePage:
        return self.page

    async def add_cookies(self, cookies: list) -> None:
        if self.cookies_exc is not None:
            raise self.cookies_exc

    async def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    async def new_context(self, **kwargs: object) -> FakeContext:
        return self.context


class FakeBrowserManager:
    concurrency = _NullConcurrency()

    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    async def ensure_browser(self) -> FakeBrowser:
        return self.browser

    def begin_fetch(self) -> None:
        return None

    def end_fetch(self) -> None:
        return None


# --- app factory ----------------------------------------------------------------


def _make_app(manager: FakeBrowserManager) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings.model_construct(
        api_tokens="secret", auth_disabled=False
    )
    app.state.browser_manager = manager
    app.state.failure_tracker = FailureTracker()
    app.include_router(extract_router)
    return app


def _client_for(page: FakePage, cookies_exc: BaseException | None = None) -> TestClient:
    manager = FakeBrowserManager(FakeBrowser(FakeContext(page, cookies_exc=cookies_exc)))
    return TestClient(_make_app(manager))


AUTH = {"Authorization": "Bearer secret"}

# renderWaitMs=0 skips the SPA render-wait entirely (deadline already passed),
# keeping these tests instant and independent of the stability clock.
_BASE_BODY = {
    "url": "https://example.test/pdp",
    "renderWaitMs": 0,
}

_PDP_LD = (
    '<script type="application/ld+json">'
    '{"@type":"Product","name":"LG Monitor",'
    '"brand":{"@type":"Brand","name":"LG"},'
    '"offers":{"@type":"Offer","price":499,"priceCurrency":"NZD",'
    '"availability":"https://schema.org/InStock"}}'
    "</script>"
)


# --- tests -----------------------------------------------------------------------


def test_extract_jsonld_hit_returns_price_and_full_product_node() -> None:
    client = _client_for(FakePage(f"<html><body>{_PDP_LD}</body></html>"))
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "source": "jsonld",
        "url": "https://example.test/pdp",
        "available": True,
        "price": "499.00",
        "currency": "NZD",
        "availability": "https://schema.org/InStock",
        "name": "LG Monitor",
        "jsonld": {
            "@type": "Product",
            "name": "LG Monitor",
            "brand": {"@type": "Brand", "name": "LG"},
            "offers": {
                "@type": "Offer",
                "price": 499,
                "priceCurrency": "NZD",
                "availability": "https://schema.org/InStock",
            },
        },
    }


def test_extract_blocked_page_short_circuits_with_signature() -> None:
    # akamai-waf deny marker: classified, retryable=False — and crucially no
    # extraction/AI work happens on deny pages.
    client = _client_for(FakePage('<a href="/WAF_Deny_Page/x">denied</a>'))
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "blocked",
        "signature": "akamai-waf",
        "retryable": False,
    }


def test_extract_no_jsonld_ai_fallback_disabled_is_extraction_failed() -> None:
    client = _client_for(FakePage("<html><body>no structured data</body></html>"))
    response = client.post(
        "/v1/extract-price",
        json={**_BASE_BODY, "aiFallback": False},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "extraction_failed",
        "signature": None,
        "retryable": None,
    }


def test_extract_no_jsonld_default_fallback_degrades_to_noop() -> None:
    # aiFallback defaults true, but Phase A mounts no AiClient on app.state:
    # the fallback degrades to a logged no-op instead of throwing (AC4).
    client = _client_for(FakePage("<html><body>no structured data</body></html>"))
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "extraction_failed",
        "signature": None,
        "retryable": None,
    }


def test_extract_navigation_timeout_maps_to_fetch_failed() -> None:
    client = _client_for(FakePage("", goto_exc=asyncio.TimeoutError()))
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["reason"] == "fetch_failed"


def test_extract_unexpected_navigation_error_maps_to_fetch_failed() -> None:
    client = _client_for(FakePage("", goto_exc=RuntimeError("browser gone")))
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["reason"] == "fetch_failed"


def test_extract_cookie_injection_failure_maps_to_fetch_failed() -> None:
    client = _client_for(
        FakePage(""),
        cookies_exc=RuntimeError("bad jar"),
    )
    response = client.post(
        "/v1/extract-price",
        json={
            **_BASE_BODY,
            "cookies": [
                {
                    "name": "session",
                    "value": "x",
                    "domain": ".example.test",
                    "path": "/",
                }
            ],
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "fetch_failed"


def test_extract_requires_bearer_token() -> None:
    client = _client_for(FakePage("<html></html>"))
    response = client.post("/v1/extract-price", json=_BASE_BODY)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_extract_rejects_relative_url() -> None:
    client = _client_for(FakePage("<html></html>"))
    response = client.post(
        "/v1/extract-price",
        json={"url": "not-absolute", "renderWaitMs": 0},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_failure_tracker_hit_counts_as_success() -> None:
    # White-box: the degradation trend reads consecutive failures; a completed
    # extraction must leave the counter at zero.
    tracker = FailureTracker()
    manager = FakeBrowserManager(
        FakeBrowser(FakeContext(FakePage(f"<html><body>{_PDP_LD}</body></html>")))
    )
    app = _make_app(manager)
    app.state.failure_tracker = tracker
    client = TestClient(app)
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.json()["ok"] is True
    assert tracker._consecutive_failures == 0


def test_failure_tracker_blocked_counts_as_success() -> None:
    # A detected block is a per-site signal, not browser degradation — same
    # semantics as /v1/fetch (error-handling.md pattern 4).
    tracker = FailureTracker()
    manager = FakeBrowserManager(
        FakeBrowser(FakeContext(FakePage('<a href="/WAF_Deny_Page/x">d</a>')))
    )
    app = _make_app(manager)
    app.state.failure_tracker = tracker
    client = TestClient(app)
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.json()["reason"] == "blocked"
    assert tracker._consecutive_failures == 0


def test_failure_tracker_timeout_counts_as_failure() -> None:
    tracker = FailureTracker()
    manager = FakeBrowserManager(
        FakeBrowser(FakeContext(FakePage("", goto_exc=asyncio.TimeoutError())))
    )
    app = _make_app(manager)
    app.state.failure_tracker = tracker
    client = TestClient(app)
    response = client.post("/v1/extract-price", json=_BASE_BODY, headers=AUTH)
    assert response.json()["reason"] == "fetch_failed"
    assert tracker._consecutive_failures == 1
