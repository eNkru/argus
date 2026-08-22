"""``POST /v1/extract-price`` — fetch a page and extract its product price.

Landed 2026-08-23 as argus's extraction sibling to ``/v1/fetch``: the caller
gets a parsed price (plus the rich schema.org Product node) in one call instead
of fetching HTML and parsing client-side. Two stages:

1. **Deterministic (no LLM):** ``jsonld.extract_offer`` parses the page's
   ``<script type="application/ld+json">`` for a schema.org Offer with a
   usable price. Verified live against pbtech/kogan/farmers PDPs — zero
   per-site selectors (the forbidden per-retailer-branching pattern).
2. **AI fallback:** on a JSON-LD miss with ``aiFallback=true``, an
   OpenAI-compatible LLM reads the page (Phase B mounts ``AiClient`` on
   ``app.state``). Blocked pages short-circuit BEFORE any LLM call — a deny
   page carries no price, and burning a model call would mask the block as
   "unavailable" (the exact mistake iris's ``checkPrice`` documents).

The navigation half is shared with ``/v1/fetch`` via ``navigate.fetch_html``;
this module owns only extraction and response mapping. Never throws to the
caller: every outcome is a well-formed ``{ok: ...}`` body.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .. import jsonld
from ..auth import require_token
from ..browser import BrowserManager
from ..diagnostics import FailureTracker
from ..navigate import HtmlBlocked, HtmlFailed, HtmlOk, fetch_html
from ..schemas import (
    ExtractPriceRequest,
    ExtractPriceResponseFail,
    ExtractPriceResponseOk,
)

logger = logging.getLogger("argus.extract")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@router.post("/extract-price", response_model=None)
async def extract_price(request: ExtractPriceRequest, req: Request):
    browser_manager: BrowserManager = req.app.state.browser_manager
    tracker: FailureTracker = req.app.state.failure_tracker
    browser_manager.begin_fetch()
    try:
        return await _do_extract(request, browser_manager, tracker, req)
    finally:
        browser_manager.end_fetch()


async def _do_extract(
    request: ExtractPriceRequest,
    browser_manager: BrowserManager,
    tracker: FailureTracker,
    req: Request,
) -> ExtractPriceResponseOk | ExtractPriceResponseFail:
    async with browser_manager.concurrency:
        try:
            result = await fetch_html(browser_manager, request, tracker=tracker)
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            tracker.record_failure(request.url, exc, kind="timeout")
            return ExtractPriceResponseFail(reason="fetch_failed")
        except Exception as exc:  # noqa: BLE001 — never throw to the caller
            tracker.record_failure(request.url, exc, kind="error")
            return ExtractPriceResponseFail(reason="fetch_failed")

        if isinstance(result, HtmlFailed):
            return ExtractPriceResponseFail(reason="fetch_failed")
        if isinstance(result, HtmlBlocked):
            # A deny page has no price and must not reach the LLM (Phase B):
            # short-circuit with the signature so the caller can retry on a
            # retryable block instead of reading "extraction_failed" as
            # "out of stock".
            return ExtractPriceResponseFail(
                reason="blocked",
                signature=result.signature,
                retryable=result.retryable,
            )

        # Only HtmlOk remains in the union.
        if not 200 <= result.status < 300:
            logger.warning(
                "extract non-2xx status (page may be an unclassified "
                "challenge) url=%s status=%d final_url=%s html_len=%d",
                request.url,
                result.status,
                result.final_url,
                len(result.html),
            )

        offer = jsonld.extract_offer(result.html)
        if offer is not None:
            return ExtractPriceResponseOk(
                source="jsonld",
                url=result.final_url,
                available=jsonld.available_from(offer.availability),
                price=offer.price,
                currency=offer.currency,
                availability=offer.availability,
                name=offer.name,
                jsonld=offer.product_node,
            )

        # JSON-LD miss. Stage 2 (AI) only when the caller opted in AND an
        # AiClient is mounted; otherwise degrade to a logged no-op — a missing
        # key must read as "no price extracted", never as a 500.
        ai_client = getattr(req.app.state, "ai_client", None)
        if request.aiFallback and ai_client is None:
            # Only worth a log line when the caller actually wanted the AI
            # stage; aiFallback=false misses are the caller's explicit choice.
            logger.info(
                "AI provider not configured (missing key) url=%s", request.url
            )
        # Phase B mounts AiClient on app.state and calls
        # ai_client.extract_price(result.html, request.url) here; until then
        # every fallback attempt resolves to extraction_failed.
        return ExtractPriceResponseFail(reason="extraction_failed")
