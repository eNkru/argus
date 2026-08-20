"""Shared test fixtures.

Tests for the pure modules (auth, schemas, cookies, signatures, health) run
without the Camoufox browser installed — they construct minimal FastAPI apps
and fake app.state objects rather than spinning up the real lifespan (which
imports camoufox).
"""

from __future__ import annotations

from typing import Callable

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from argus.auth import require_token
from argus.config import Settings


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Build a Settings via model_construct so no env is read."""

    def _factory(*, tokens: str = "", auth_disabled: bool = False) -> Settings:
        return Settings.model_construct(
            api_tokens=tokens, auth_disabled=auth_disabled
        )

    return _factory


@pytest.fixture
def make_auth_app() -> Callable[..., FastAPI]:
    """Build a tiny app with one bearer-guarded route."""

    def _factory(settings: Settings) -> FastAPI:
        app = FastAPI()
        app.state.settings = settings

        @app.get("/secure", dependencies=[Depends(require_token)])
        async def secure() -> dict:
            return {"ok": True}

        return app

    return _factory


@pytest.fixture
def client(make_auth_app, make_settings) -> TestClient:
    """Client whose guarded app accepts the single token ``secret``."""
    settings = make_settings(tokens="secret")
    app = make_auth_app(settings)
    return TestClient(app)
