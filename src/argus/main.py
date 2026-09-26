"""Argus FastAPI application + lifespan.

The lifespan constructs the singleton ``Settings``, ``BrowserManager``,
``FailureTracker``, and (when ``ARGUS_AI_*`` is fully configured) ``AiClient``
on ``app.state`` (so route handlers read them from there rather than re-parsing
env per request), starts the idle watcher, and tears the browser down on
shutdown. Routers are mounted under their own prefixes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .ai import build_ai_client
from .browser import BrowserManager
from .config import Settings
from .diagnostics import FailureTracker
from .ratelimit import RateLimitMiddleware
from .routes import extract, fetch, fetch_image, health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# Settings are constructed once at module scope (not re-parsed per request, and
# not re-constructed in the lifespan) so both the FastAPI() docs kwargs and the
# lifespan-mounted app.state share one source of truth. Reading env at import is
# identical to reading it at lifespan-start for a single process.
_settings = Settings()

# Interactive API docs are off by default (production-safe). dev.sh exports
# ARGUS_DOCS_ENABLED=true to keep /docs locally.
_docs_kwargs = (
    {}
    if _settings.docs_enabled
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = _settings
    app.state.settings = settings
    app.state.browser_manager = BrowserManager(settings)
    app.state.failure_tracker = FailureTracker()
    # None when ARGUS_AI_* is unconfigured: /v1/extract-price degrades to a
    # JSON-LD-only service (a missing key must never read as a server error).
    app.state.ai_client = build_ai_client(settings)

    await app.state.browser_manager.start()
    try:
        yield
    finally:
        await app.state.browser_manager.stop()
        if app.state.ai_client is not None:
            await app.state.ai_client.aclose()


app = FastAPI(title="Argus", lifespan=lifespan, **_docs_kwargs)
# Rate limiting wraps the whole app (before auth), so the 401 bearer-brute-force
# path is throttled like any /v1/* request. No-op while rate_limit_enabled=False.
app.add_middleware(RateLimitMiddleware, settings=_settings)
app.include_router(health.router)
app.include_router(fetch.router)
app.include_router(extract.router)
app.include_router(fetch_image.router)
