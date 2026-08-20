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
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..auth import require_token
from ..browser import BrowserManager
from ..cookies import to_playwright_cookies
from ..diagnostics import FailureTracker
from ..render import snapshot_content, wait_for_render
from ..schemas import (
    FetchRequest,
    FetchResponseFail,
    FetchResponseOk,
)
from ..signatures import detect, is_retryable

logger = logging.getLogger("argus.fetch")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@router.post("/fetch", response_model=None)
async def fetch(request: FetchRequest, req: Request):
    browser_manager: BrowserManager = req.app.state.browser_manager
    tracker: FailureTracker = req.app.state.failure_tracker
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
            browser = await browser_manager.ensure_browser()

            # Fresh ephemeral context per request — the isolated cookie jar.
            ctx_kwargs: dict = {}
            if request.locale:
                ctx_kwargs["locale"] = request.locale
            if request.userAgent:
                ctx_kwargs["user_agent"] = request.userAgent
            ctx = await browser.new_context(**ctx_kwargs)
            try:
                if request.cookies:
                    # Cookies are secrets: log count only, never names/values.
                    logger.debug(
                        "injecting cookies url=%s count=%d",
                        request.url,
                        len(request.cookies),
                    )
                    try:
                        await ctx.add_cookies(
                            to_playwright_cookies(request.cookies)
                        )
                    except Exception as exc:  # noqa: BLE001 — never throw to the caller
                        logger.warning(
                            "cookie injection failed url=%s error_type=%s error=%s",
                            request.url,
                            type(exc).__name__,
                            exc,
                        )
                        tracker.record_failure(
                            request.url, exc, kind="cookie_injection"
                        )
                        return FetchResponseFail(reason="fetch_failed")

                page = await ctx.new_page()
                try:
                    response = await page.goto(
                        request.url,
                        wait_until=request.waitUntil,
                        timeout=request.timeoutMs,
                    )
                    if response is None:
                        # page.goto returns None for navigations cancelled or
                        # redirected before a response.
                        tracker.record_failure(request.url, None, kind="no_response")
                        return FetchResponseFail(reason="fetch_failed")

                    # SPA render wait: client-rendered pages inject content via
                    # JS after domcontentloaded. Best-effort; never raises.
                    await wait_for_render(
                        page, render_wait_seconds=request.renderWaitMs / 1000
                    )

                    # Always snapshot the rendered HTML — including non-2xx
                    # challenge/deny pages — so classification can run.
                    html = await snapshot_content(page, url=request.url)
                    final_url = page.url

                    if not response.ok:
                        logger.warning(
                            "fetch non-2xx status (returning HTML for "
                            "classification) url=%s status=%d final_url=%s "
                            "html_len=%d",
                            request.url,
                            response.status,
                            final_url,
                            len(html),
                        )

                    if request.detectBlocked:
                        signature = detect(html, enabled=True)
                        if signature is not None:
                            # A response with content counts as success for the
                            # degradation trend: a block is a per-site signal,
                            # not a shared-browser-degradation signal.
                            tracker.record_success()
                            return FetchResponseFail(
                                reason="blocked",
                                signature=signature,
                                retryable=is_retryable(signature),
                            )

                    tracker.record_success()
                    return FetchResponseOk(html=html, url=final_url)
                finally:
                    try:
                        await page.close()
                    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the fetch result
                        logger.warning(
                            "page close failed url=%s error_type=%s error=%s",
                            request.url,
                            type(exc).__name__,
                            exc,
                        )
            finally:
                try:
                    await ctx.close()
                except Exception as exc:  # noqa: BLE001 — cleanup must not mask the fetch result
                    logger.warning(
                        "context close failed url=%s error_type=%s error=%s",
                        request.url,
                        type(exc).__name__,
                        exc,
                    )
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            tracker.record_failure(request.url, exc, kind="timeout")
            return FetchResponseFail(reason="fetch_failed")
        except Exception as exc:  # noqa: BLE001 — never throw to the caller
            tracker.record_failure(request.url, exc, kind="error")
            return FetchResponseFail(reason="fetch_failed")
