"""Inbound rate-limit middleware — pure offline tests.

Covers ``_WindowCounter`` fixed-window / eviction / key-isolation logic against
a fake monotonic clock, plus the ASGI middleware against a minimal in-process
FastAPI app (house style: never import ``main`` / spin up the real lifespan or
Camoufox). ``Settings.model_construct(...)`` bypasses env parsing.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from argus import ratelimit
from argus.auth import require_token
from argus.config import Settings


class _FakeClock:
    """Mutable monotonic-clock double for deterministic window rollovers."""

    def __init__(self) -> None:
        self.now = 0.0

    def tick(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


# --- _WindowCounter (pure, no HTTP) -------------------------------------------


def test_counter_under_limit() -> None:
    counter = ratelimit._WindowCounter(window_s=60.0, clock=_FakeClock())
    allowed, count, _ = counter.check("10.0.0.1", limit=2)
    assert allowed is True
    assert count == 1
    allowed, count, _ = counter.check("10.0.0.1", limit=2)
    assert allowed is True
    assert count == 2


def test_counter_over_limit() -> None:
    counter = ratelimit._WindowCounter(window_s=60.0, clock=_FakeClock())
    counter.check("10.0.0.1", limit=2)
    counter.check("10.0.0.1", limit=2)
    allowed, count, retry_after = counter.check("10.0.0.1", limit=2)
    assert allowed is False
    assert count == 3
    assert retry_after >= 1


def test_counter_resets_after_window() -> None:
    clock = _FakeClock()
    counter = ratelimit._WindowCounter(window_s=60.0, clock=clock)
    counter.check("10.0.0.1", limit=2)
    counter.check("10.0.0.1", limit=2)
    assert counter.check("10.0.0.1", limit=2)[0] is False

    clock.tick(60.0)  # window rolls over → budget resets
    allowed, count, _ = counter.check("10.0.0.1", limit=2)
    assert allowed is True
    assert count == 1


def test_counter_key_isolation() -> None:
    counter = ratelimit._WindowCounter(window_s=60.0, clock=_FakeClock())
    counter.check("10.0.0.1", limit=1)
    assert counter.check("10.0.0.1", limit=1)[0] is False
    # A different IP has its own window/budget.
    allowed, count, _ = counter.check("10.0.0.2", limit=1)
    assert allowed is True
    assert count == 1


# --- RateLimitMiddleware (minimal app) -----------------------------------------


def _make_app(*, enabled: bool, requests: int, window_s: int) -> FastAPI:
    settings = Settings.model_construct(
        rate_limit_enabled=enabled,
        rate_limit_requests=requests,
        rate_limit_window_s=window_s,
    )
    app = FastAPI()
    app.add_middleware(ratelimit.RateLimitMiddleware, settings=settings)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


def test_under_limit_200_over_limit_429() -> None:
    client = TestClient(_make_app(enabled=True, requests=2, window_s=60))
    assert client.get("/x").status_code == 200
    assert client.get("/x").status_code == 200

    third = client.get("/x")
    assert third.status_code == 429
    assert third.json() == {"detail": "rate limit exceeded"}
    assert int(third.headers["retry-after"]) >= 1


def test_health_exempt_and_does_not_consume_budget() -> None:
    client = TestClient(_make_app(enabled=True, requests=2, window_s=60))
    assert client.get("/x").status_code == 200
    assert client.get("/x").status_code == 200

    # /health always 200 and does not consume /x's budget; the 3rd /x is still
    # over the limit.
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/x").status_code == 429


def test_health_never_throttled() -> None:
    client = TestClient(_make_app(enabled=True, requests=1, window_s=60))
    assert client.get("/x").status_code == 200  # exhaust the one-request budget
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_disabled_is_pass_through() -> None:
    client = TestClient(_make_app(enabled=False, requests=1, window_s=60))
    for _ in range(5):
        assert client.get("/x").status_code == 200


def test_401_path_is_rate_limited() -> None:
    """Middleware runs before auth, so the bearer brute-force path is throttled."""
    settings = Settings.model_construct(
        api_tokens="secret",
        auth_disabled=False,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_s=60,
    )
    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(ratelimit.RateLimitMiddleware, settings=settings)

    @app.get("/v1/x", dependencies=[Depends(require_token)])
    async def v1_x() -> dict:
        return {"ok": True}

    client = TestClient(app)
    # No Authorization header → auth 401 for the first two (under budget)...
    assert client.get("/v1/x").status_code == 401
    assert client.get("/v1/x").status_code == 401
    # ...then the middleware rejects before auth ever runs.
    assert client.get("/v1/x").status_code == 429