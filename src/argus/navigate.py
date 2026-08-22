"""Shared navigation pipeline for the HTML-fetching routes.

Extracted 2026-08-23 when ``POST /v1/extract-price`` landed: both ``/v1/fetch``
and the extractor need the identical sequence — ensure browser → fresh
ephemeral context (the isolated cookie jar) → optional cookie injection →
``goto`` → SPA render-wait → resilient content snapshot → blocked-signature
classification. Duplicating ~70 lines of lifecycle-critical cleanup across two
routes was the bug waiting to happen (a ``finally`` forgotten in one copy would
leak cookie jars between callers — the security invariant in
``database-guidelines.md``).

The helper owns the Playwright lifecycle and the failure bookkeeping
(``tracker.record_failure`` kinds preserved verbatim from the pre-refactor
``routes/fetch.py``); each route maps the returned union onto its own response
models. Transport exceptions (timeout, Playwright errors) propagate — the
calling route owns the exception ladder exactly as before, so failure mapping
stays in one visible place per route.

Result union:
- ``HtmlOk`` — rendered HTML + final URL + HTTP status (non-2xx is NOT a
  failure here: challenge pages must reach classification; routes log it).
- ``HtmlBlocked`` — a WAF challenge/deny page was detected and classified.
  Counts as ``record_success`` for the degradation trend (per-site signal).
- ``HtmlFailed`` — transport-level failure already recorded on the tracker
  (cookie injection, no navigation response).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .browser import BrowserManager
from .cookies import to_playwright_cookies
from .diagnostics import FailureTracker
from .render import snapshot_content, wait_for_render
from .signatures import detect, is_retryable

logger = logging.getLogger("argus.navigate")


class NavigateRequest(Protocol):
    """Attributes the shared pipeline reads off a request model.

    Satisfied by both ``FetchRequest`` and ``ExtractPriceRequest`` (and their
    test doubles) — duck-typed so tests can pass simple fakes.
    """

    url: str
    locale: str | None
    userAgent: str | None
    cookies: list | None
    waitUntil: str
    timeoutMs: int
    renderWaitMs: int
    detectBlocked: bool


@dataclass
class HtmlOk:
    html: str
    final_url: str
    status: int


@dataclass
class HtmlBlocked:
    signature: str
    retryable: bool


@dataclass
class HtmlFailed:
    """Transport-level failure; the tracker already recorded it."""


async def fetch_html(
    browser_manager: BrowserManager,
    request: NavigateRequest,
    *,
    tracker: FailureTracker,
) -> HtmlOk | HtmlBlocked | HtmlFailed:
    """Navigate to ``request.url`` and return the rendered-HTML outcome.

    Raises nothing for transport problems it classifies itself (cookie
    injection, no navigation response → ``HtmlFailed``); Playwright
    timeouts/errors from navigation propagate to the caller's exception
    ladder, preserving the pre-refactor failure mapping.
    """
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
                await ctx.add_cookies(to_playwright_cookies(request.cookies))
            except Exception as exc:  # noqa: BLE001 — never throw to the caller
                logger.warning(
                    "cookie injection failed url=%s error_type=%s error=%s",
                    request.url,
                    type(exc).__name__,
                    exc,
                )
                tracker.record_failure(request.url, exc, kind="cookie_injection")
                return HtmlFailed()

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
                return HtmlFailed()

            # SPA render wait: client-rendered pages inject content via
            # JS after domcontentloaded. Best-effort; never raises.
            await wait_for_render(
                page, render_wait_seconds=request.renderWaitMs / 1000
            )

            # Always snapshot the rendered HTML — including non-2xx
            # challenge/deny pages — so classification can run.
            html = await snapshot_content(page, url=request.url)
            final_url = page.url

            if request.detectBlocked:
                signature = detect(html, enabled=True)
                if signature is not None:
                    # A response with content counts as success for the
                    # degradation trend: a block is a per-site signal, not a
                    # shared-browser-degradation signal.
                    tracker.record_success()
                    return HtmlBlocked(
                        signature=signature,
                        retryable=is_retryable(signature),
                    )

            tracker.record_success()
            return HtmlOk(html=html, final_url=final_url, status=response.status)
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
