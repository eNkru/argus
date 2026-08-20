"""Shared Camoufox browser: lazy launch, single-flight, idle teardown.

Ported from the iris sidecar's module-global lifecycle, reorganized into a
class so the lifespan owns one ``BrowserManager`` on ``app.state`` instead of
spraying globals across the process.

Design:

- The browser is **not** launched at boot. It launches lazily on the first
  fetch (``ensure_browser``) under a single-flight lock, so N concurrent
  first-fetches produce exactly one launch.
- An idle watcher tears the browser down after ``idle_timeout_seconds`` with no
  fetch in flight, reclaiming the ~350-500 MB Firefox process tree between
  scrapes on lightly-loaded hosts.
- An in-flight counter prevents tearing down a browser mid-navigation.

``AsyncCamoufox`` is the async context-manager client that launches the
anti-detect Firefox. Entering it yields a Playwright ``Browser`` (the context
manager itself does not expose ``new_page`` / ``new_context``), so we hold both
the context (for orderly shutdown) and the yielded browser (for per-request
contexts).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser

from .config import Settings

logger = logging.getLogger("argus.browser")

# How often the idle watcher polls. 30 s balances teardown promptness against
# trivial per-poll cost.
_IDLE_POLL_SECONDS = 30.0


class BrowserManager:
    """Owns the single shared Camoufox browser and its lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._launch_lock: Optional[asyncio.Lock] = None
        self._camoufox_ctx: Optional[AsyncCamoufox] = None
        self._browser: Optional[Browser] = None
        # In-flight fetch counter so the idle watcher never tears down a browser
        # while a navigation is mid-flight. Incremented on fetch entry,
        # decremented in a finally. The semaphore bounds concurrency (5); this
        # counter gates teardown.
        self._active_fetches: int = 0
        self._last_activity_at: float = time.monotonic()
        self._idle_task: Optional[asyncio.Task[None]] = None

    @property
    def is_ready(self) -> bool:
        """True when the shared browser is currently resident."""
        return self._browser is not None

    @property
    def concurrency(self) -> asyncio.Semaphore:
        """The asyncio semaphore bounding concurrent fetches."""
        assert self._semaphore is not None, "BrowserManager not started"
        return self._semaphore

    async def start(self) -> None:
        """Lifespan startup: semaphore, launch lock, idle watcher.

        The browser is NOT launched here — it launches lazily on the first
        fetch and tears down after the idle timeout.
        """
        self._semaphore = asyncio.Semaphore(self._settings.concurrency)
        self._launch_lock = asyncio.Lock()
        self._last_activity_at = time.monotonic()
        self._idle_task = asyncio.create_task(self._idle_watcher())
        logger.info(
            "BrowserManager ready (browser launches on first fetch, "
            "idle teardown after %ss)",
            self._settings.idle_timeout_seconds,
        )

    async def stop(self) -> None:
        """Lifespan shutdown: cancel the idle watcher, tear the browser down."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        if self._camoufox_ctx is not None:
            logger.info("Closing Camoufox browser (shutdown)")
            try:
                await self._camoufox_ctx.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — shutdown cleanup must not mask exit
                logger.warning("Camoufox browser shutdown error: %s", exc)
        self._camoufox_ctx = None
        self._browser = None
        self._launch_lock = None
        self._semaphore = None

    async def ensure_browser(self) -> Browser:
        """Lazily launch the shared browser, or return the live one.

        Fast path: if the browser is already up, return it. Otherwise acquire
        the launch lock (single-flight) and, after re-checking under the lock,
        enter a fresh ``AsyncCamoufox`` context and store the yielded Browser.
        Concurrent callers block on the lock and reuse the one launch.

        On any exception, partial state is cleared so the next request retries
        from a clean ABSENT state, and the exception propagates to the caller's
        handler (which records a failure and returns ``fetch_failed``).
        """
        if self._browser is not None:
            return self._browser
        assert self._launch_lock is not None, "BrowserManager not started"
        async with self._launch_lock:
            # Double-checked locking: another waiter may have launched while we
            # waited for the lock.
            if self._browser is not None:
                return self._browser
            logger.info(
                "Launching shared Camoufox browser (lazy, %s fingerprint)",
                self._settings.default_fingerprint_os,
            )
            ctx = AsyncCamoufox(
                headless=True, os=self._settings.default_fingerprint_os
            )
            try:
                browser = await ctx.__aenter__()
            except BaseException:
                # Clear half-set state so the next attempt starts clean. Do not
                # call __aexit__ here: __aenter__ failed, so there is nothing to
                # exit.
                self._camoufox_ctx = None
                self._browser = None
                raise
            self._camoufox_ctx = ctx
            self._browser = browser
            logger.info("Camoufox browser ready")
            return self._browser

    async def teardown_if_idle(self) -> None:
        """Tear the browser down after the idle timeout with no fetches in flight.

        No-op when the browser is already absent, when the idle threshold has not
        elapsed, or when a fetch is in-flight. Acquires the launch lock so it
        cannot race a concurrent lazy launch.
        """
        if self._browser is None:
            return
        if time.monotonic() - self._last_activity_at < self._settings.idle_timeout_seconds:
            return
        if self._active_fetches > 0:
            return
        assert self._launch_lock is not None, "BrowserManager not started"
        async with self._launch_lock:
            # Re-check under the lock: a fetch may have entered between the
            # unlocked check and acquiring the lock.
            if self._browser is None or self._active_fetches > 0:
                return
            if time.monotonic() - self._last_activity_at < self._settings.idle_timeout_seconds:
                return
            logger.info(
                "Idle timeout reached (%.0fs), closing Camoufox browser",
                self._settings.idle_timeout_seconds,
            )
            ctx = self._camoufox_ctx
            # Null the handles first so a fetch arriving while __aexit__ runs
            # sees ABSENT and launches fresh, rather than reusing a browser
            # being torn down.
            self._browser = None
            self._camoufox_ctx = None
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 — teardown failure must not crash the watcher
                logger.warning("Camoufox browser teardown error: %s", exc)
        logger.info("Camoufox browser closed (idle teardown)")

    async def _idle_watcher(self) -> None:
        """Background loop polling for idle teardown."""
        while True:
            await asyncio.sleep(_IDLE_POLL_SECONDS)
            await self.teardown_if_idle()

    def begin_fetch(self) -> None:
        """Mark a fetch as in-flight (gates teardown). Call before navigating."""
        self._last_activity_at = time.monotonic()
        self._active_fetches += 1

    def end_fetch(self) -> None:
        """Mark a fetch as complete. Always paired with begin_fetch in a finally."""
        self._last_activity_at = time.monotonic()
        self._active_fetches -= 1
