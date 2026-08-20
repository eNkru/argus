"""Bearer-token auth: the only gate on /v1/* routes."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_missing_token_returns_401(client: TestClient) -> None:
    response = client.get("/secure")
    assert response.status_code == 401


def test_non_bearer_scheme_returns_401(client: TestClient) -> None:
    response = client.get("/secure", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401


def test_invalid_token_returns_401(client: TestClient) -> None:
    response = client.get(
        "/secure", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_valid_token_returns_200(client: TestClient) -> None:
    response = client.get(
        "/secure", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_multiple_tokens_any_one_works(make_auth_app, make_settings) -> None:
    settings = make_settings(tokens="alpha,beta")
    app = make_auth_app(settings)
    c = TestClient(app)
    assert c.get("/secure", headers={"Authorization": "Bearer alpha"}).status_code == 200
    assert c.get("/secure", headers={"Authorization": "Bearer beta"}).status_code == 200
    assert c.get("/secure", headers={"Authorization": "Bearer gamma"}).status_code == 401


def test_auth_disabled_bypasses(make_auth_app, make_settings) -> None:
    settings = make_settings(tokens="secret", auth_disabled=True)
    app = make_auth_app(settings)
    c = TestClient(app)
    # No Authorization header at all, yet allowed.
    assert c.get("/secure").status_code == 200
