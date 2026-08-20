"""``POST /v1/fetch-image`` — fetch a binary image through the browser.

Product image URLs often sit behind the same anti-bot WAF as the product page.
Routing the image download through the browser ensures the request carries the
full anti-detect fingerprint and passes the WAF. Supports the same optional
``cookies`` / ``locale`` / ``userAgent`` as ``/v1/fetch`` so an image URL that
requires a logged-in session can be fetched with the caller's cookie jar.

Images don't need the SPA render-wait or content-snapshot retry —
``wait_until="load"`` fires when the image bytes are fully received. Returns
base64-encoded binary data (JSON cannot carry raw bytes).
"""

from __future__ import annotations

import asyncio
import base64
import logging

from fastapi import APIRouter, Depends, Request
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..auth import require_token
from ..browser import BrowserManager
from ..cookies import to_playwright_cookies
from ..diagnostics import FailureTracker
from ..schemas import (
    FetchImageRequest,
    FetchImageResponseFail,
    FetchImageResponseOk,
)

logger = logging.getLogger("argus.fetch_image")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@router.post("/fetch-image", response_model=None)
async def fetch_image(request: FetchImageRequest, req: Request):
    browser_manager: BrowserManager = req.app.state.browser_manager
    tracker: FailureTracker = req.app.state.failure_tracker
    browser_manager.begin_fetch()
    try:
        return await _do_fetch_image(request, browser_manager, tracker)
    finally:
        browser_manager.end_fetch()


async def _do_fetch_image(
    request: FetchImageRequest,
    browser_manager: BrowserManager,
    tracker: FailureTracker,
) -> FetchImageResponseOk | FetchImageResponseFail:
    async with browser_manager.concurrency:
        try:
            browser = await browser_manager.ensure_browser()

            ctx_kwargs: dict = {}
            if request.locale:
                ctx_kwargs["locale"] = request.locale
            if request.userAgent:
                ctx_kwargs["user_agent"] = request.userAgent
            ctx = await browser.new_context(**ctx_kwargs)
            try:
                if request.cookies:
                    try:
                        await ctx.add_cookies(
                            to_playwright_cookies(request.cookies)
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cookie injection failed url=%s error_type=%s error=%s",
                            request.url,
                            type(exc).__name__,
                            exc,
                        )
                        tracker.record_failure(
                            request.url, exc, kind="cookie_injection"
                        )
                        return FetchImageResponseFail(reason="fetch_failed")

                page = await ctx.new_page()
                try:
                    response = await page.goto(
                        request.url,
                        wait_until="load",
                        timeout=request.timeoutMs,
                    )
                    if response is None:
                        tracker.record_failure(request.url, None, kind="no_response")
                        return FetchImageResponseFail(reason="fetch_failed")

                    content_type = response.headers.get("content-type", "image/jpeg")

                    if not content_type.startswith("image/"):
                        logger.warning(
                            "fetch-image non-image content-type url=%s "
                            "content_type=%s status=%d",
                            request.url,
                            content_type,
                            response.status,
                        )
                        return FetchImageResponseFail(reason="non_image")

                    body = await response.body()
                    tracker.record_success()
                    data = base64.b64encode(body).decode("ascii")
                    return FetchImageResponseOk(
                        contentType=content_type, data=data
                    )
                finally:
                    try:
                        await page.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "page close failed url=%s error_type=%s error=%s",
                            request.url,
                            type(exc).__name__,
                            exc,
                        )
            finally:
                try:
                    await ctx.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "context close failed url=%s error_type=%s error=%s",
                        request.url,
                        type(exc).__name__,
                        exc,
                    )
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            tracker.record_failure(request.url, exc, kind="timeout")
            return FetchImageResponseFail(reason="fetch_failed")
        except Exception as exc:  # noqa: BLE001 — never throw to the caller
            tracker.record_failure(request.url, exc, kind="error")
            return FetchImageResponseFail(reason="fetch_failed")
