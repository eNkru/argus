"""Argus FastAPI application + lifespan.

The lifespan constructs the singleton ``Settings``, ``BrowserManager``, and
``FailureTracker`` on ``app.state`` (so route handlers read them from there
rather than re-parsing env per request), starts the idle watcher, and tears the
browser down on shutdown. Routers are mounted under their own prefixes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .browser import BrowserManager
from .config import Settings
from .diagnostics import FailureTracker
from .routes import fetch, fetch_image, health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    app.state.settings = settings
    app.state.browser_manager = BrowserManager(settings)
    app.state.failure_tracker = FailureTracker()

    await app.state.browser_manager.start()
    try:
        yield
    finally:
        await app.state.browser_manager.stop()


app = FastAPI(title="Argus", lifespan=lifespan)
app.include_router(health.router)
app.include_router(fetch.router)
app.include_router(fetch_image.router)
