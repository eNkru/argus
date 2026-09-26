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
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .. import jsonld
from ..ai import PriceExtraction
from ..auth import require_token
from ..browser import BrowserManager
from ..diagnostics import FailureTracker
from ..navigate import HtmlBlocked, HtmlFailed, HtmlOk, fetch_html
from ..urlguard import guard_url
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
    if guard_url(request.url, req.app.state.settings):
        return ExtractPriceResponseFail(reason="fetch_failed")
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
        extraction: PriceExtraction | None = None
        if request.aiFallback and ai_client is not None:
            # AI-extracted prices are UNTRUSTED (security-guidelines.md): the
            # prompt is fed attacker-controlled page content. An operator may
            # scope the LLM stage to trusted retailer hosts via
            # ARGUS_AI_EXTRACT_DOMAIN_ALLOWLIST. Empty (default) = no
            # restriction, byte-identical to pre-allowlist behavior. A
            # non-matching final host degrades to extraction_failed with an
            # INFO log — no LLM call, no cost.
            allowlist = req.app.state.settings.ai_extract_domain_allowlist_set
            if allowlist:
                host = (urlparse(result.final_url).hostname or "").lower()
                if not any(host == s or host.endswith("." + s) for s in allowlist):
                    logger.info(
                        "ai_stage_skipped url=%s reason=host_not_allowlisted",
                        request.url,
                    )
                    return ExtractPriceResponseFail(reason="extraction_failed")
            # ai.py never throws: provider errors, retries-exhausted, and
            # schema mismatches all surface here as None.
            extraction = await ai_client.extract_price(result.html, request.url)
        elif request.aiFallback:
            # Only worth a log line when the caller actually wanted the AI
            # stage; aiFallback=false misses are the caller's explicit choice.
            logger.info(
                "AI provider not configured (missing key) url=%s", request.url
            )
        if extraction is not None:
            # The AI path has no JSON-LD to return — that's why it fell back —
            # so the rich node stays null and only the flat verdict ships.
            return ExtractPriceResponseOk(
                source="ai",
                url=result.final_url,
                available=extraction.available,
                price=extraction.price,
                currency=extraction.currency,
                availability=None,
                name=extraction.name,
                jsonld=None,
            )
        return ExtractPriceResponseFail(reason="extraction_failed")
