"""``POST /v1/fetch`` — fetch a page's rendered HTML.

Generalized from the iris sidecar: every fetch runs in a fresh ephemeral
``BrowserContext`` (instead of ``browser.new_page()`` directly) so caller-
supplied cookies, locale, and user-agent can be attached before navigation.
The shared browser is one process; the context is the isolated cookie jar,
destroyed in a ``finally`` so no cookie/storage leaks between callers.

The service never throws to the caller: navigation/timeout errors map to
``{ok:false, reason:"fetch_failed"}``. When navigation produces a response
(including non-2xx challenge/deny pages), the HTML is always returned so the
blocked-signature registry (or the caller's own, via ``detectBlocked:false``)
can classify it.

The Playwright lifecycle itself lives in ``navigate.fetch_html`` — shared with
``POST /v1/extract-price`` since 2026-08-23. This module owns the response
mapping and the exception ladder; behavior is unchanged by the extraction.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..auth import require_token
from ..browser import BrowserManager
from ..diagnostics import FailureTracker
from ..navigate import HtmlBlocked, HtmlFailed, HtmlOk, fetch_html
from ..urlguard import guard_url
from ..schemas import (
    FetchRequest,
    FetchResponseFail,
    FetchResponseOk,
)

logger = logging.getLogger("argus.fetch")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@router.post("/fetch", response_model=None)
async def fetch(request: FetchRequest, req: Request):
    browser_manager: BrowserManager = req.app.state.browser_manager
    tracker: FailureTracker = req.app.state.failure_tracker
    if guard_url(request.url, req.app.state.settings):
        return FetchResponseFail(reason="fetch_failed")
    browser_manager.begin_fetch()
    try:
        return await _do_fetch(request, browser_manager, tracker)
    finally:
        browser_manager.end_fetch()


async def _do_fetch(
    request: FetchRequest,
    browser_manager: BrowserManager,
    tracker: FailureTracker,
) -> FetchResponseOk | FetchResponseFail:
    async with browser_manager.concurrency:
        try:
            result = await fetch_html(browser_manager, request, tracker=tracker)
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            tracker.record_failure(request.url, exc, kind="timeout")
            return FetchResponseFail(reason="fetch_failed")
        except Exception as exc:  # noqa: BLE001 — never throw to the caller
            tracker.record_failure(request.url, exc, kind="error")
            return FetchResponseFail(reason="fetch_failed")

        if isinstance(result, HtmlFailed):
            return FetchResponseFail(reason="fetch_failed")
        if isinstance(result, HtmlBlocked):
            return FetchResponseFail(
                reason="blocked",
                signature=result.signature,
                retryable=result.retryable,
            )
        # Only HtmlOk remains in the union.
        if not 200 <= result.status < 300:
            logger.warning(
                "fetch non-2xx status (returning HTML for "
                "classification) url=%s status=%d final_url=%s "
                "html_len=%d",
                request.url,
                result.status,
                result.final_url,
                len(result.html),
            )

        return FetchResponseOk(html=result.html, url=result.final_url)
