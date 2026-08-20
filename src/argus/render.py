"""Page-level helpers: SPA render-wait and resilient content snapshot.

Ported verbatim from the iris sidecar. Two concerns:

- ``wait_for_render``: client-rendered SPAs inject their content via JS *after*
  ``domcontentloaded``. Snapshotting ``page.content()`` at that event yields an
  empty shell. This polls ``document.body.innerText.length`` until it stabilizes
  (bounded by a cap) so the real content is present before snapshotting. Never
  raises — a failure to evaluate is treated as "no more waiting" so the existing
  timeout / exception paths remain the only sources of ``fetch_failed``.

- ``snapshot_content``: ``page.content()`` raises when the page is mid-
  navigation / redirect at the instant of the snapshot (common on sites whose
  WAF rewrites the document in-flight after ``domcontentloaded``). A short
  bounded wait-and-retry settles the navigation; only a persistent failure
  escalates to the caller's exception handler.
"""

from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger("argus.render")

# Minimum body.innerText length before the stability clock starts. SPA shells
# often render a tiny partial (e.g. 9 chars of chrome) before the real content
# hydrates; without this floor the wait would "stabilize" on the empty stub.
RENDER_MIN_TEXT_LEN = 200
# How long body.innerText.length must stay unchanged (once above the floor)
# before we treat the page as rendered.
RENDER_STABLE_SECONDS = 1.0

# `page.content()` snapshot retries when the page is mid-navigation. The
# navigation settles within a few hundred ms; 3 attempts at 400 ms covers a slow
# rewrite without approaching the per-request navigation timeout.
CONTENT_RETRY_ATTEMPTS = 3
CONTENT_RETRY_DELAY_SECONDS = 0.4


async def wait_for_render(page: object, *, render_wait_seconds: float) -> None:
    """Best-effort wait for SPA content to render after domcontentloaded.

    Algorithm:
      1. Poll ``document.body.innerText.length`` every 100 ms.
      2. Ignore lengths below ``RENDER_MIN_TEXT_LEN`` (SPA chrome stubs must not
         "stabilize" the wait early).
      3. Once above the floor, return when the length is unchanged for
         ``RENDER_STABLE_SECONDS``, or when ``render_wait_seconds`` elapses.

    Never raises. Generic — no per-site selectors.
    """
    deadline = time.monotonic() + render_wait_seconds
    last_len: int | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            current = await page.evaluate(  # type: ignore[attr-defined]
                "document.body && document.body.innerText "
                "? document.body.innerText.length : 0"
            )
        except Exception:  # noqa: BLE001 — best-effort; never fail the fetch
            return
        if not isinstance(current, int):
            current = 0
        if current < RENDER_MIN_TEXT_LEN:
            # Still below the floor — do not start the stability clock.
            last_len = None
            stable_since = None
        elif current == last_len:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= RENDER_STABLE_SECONDS:
                return
        else:
            last_len = current
            stable_since = None
        await asyncio.sleep(0.1)
    # Cap hit — proceed with whatever is currently rendered.


async def snapshot_content(page: object, *, url: str) -> str:
    """Read ``page.content()`` resiliently.

    On the transient "page is navigating and changing the content" error, wait
    ``CONTENT_RETRY_DELAY_SECONDS`` and retry, up to ``CONTENT_RETRY_ATTEMPTS``
    times. Only a persistent failure escalates to the caller's handler (which
    maps it to ``fetch_failed``). Never raises on the happy path.
    """
    for attempt in range(1, CONTENT_RETRY_ATTEMPTS + 1):
        try:
            return await page.content()  # type: ignore[attr-defined]
        except PlaywrightError as exc:
            transient = "navigating and changing the content" in str(exc)
            if not transient or attempt >= CONTENT_RETRY_ATTEMPTS:
                raise
            logger.info(
                "page content mid-navigation, retrying url=%s attempt=%d/%d",
                url,
                attempt,
                CONTENT_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(CONTENT_RETRY_DELAY_SECONDS)
    # Unreachable: the loop either returns or raises on the last attempt.
    raise RuntimeError("content snapshot loop exited without a result")
