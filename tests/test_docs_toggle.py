"""``ARGUS_DOCS_ENABLED`` toggle — production hides /docs, /redoc, /openapi.json.

``main.py`` builds the FastAPI app with ``docs_url``/``redoc_url``/``openapi_url``
set to ``None`` unless ``Settings.docs_enabled`` is true (``ARGUS_DOCS_ENABLED``).
This test exercises the exact conditional ``main.py`` uses, on a purpose-built
app (the house style: never spin up the real lifespan / import camoufox).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus.config import Settings


def _app(docs_enabled: bool) -> FastAPI:
    settings = Settings.model_construct(docs_enabled=docs_enabled)
    # Mirrors main.py's _docs_kwargs exactly.
    docs_kwargs = (
        {}
        if settings.docs_enabled
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    app = FastAPI(title="x", **docs_kwargs)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_docs_disabled_by_default_returns_404() -> None:
    client = TestClient(_app(docs_enabled=False))
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    # /health is a real route, unaffected by the docs toggle.
    assert client.get("/health").status_code == 200


def test_docs_enabled_serves_docs_and_schema() -> None:
    client = TestClient(_app(docs_enabled=True))
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
