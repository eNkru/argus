"""AI price-extraction fallback — generic OpenAI-compatible, raw httpx.

Stage 2 of ``POST /v1/extract-price``: when the deterministic JSON-LD parse
(``jsonld.py``) finds no usable price, an LLM reads a reduced version of the
page and returns ``{price, currency, name, available}``. Ported from iris's
proven ``packages/prices/src/pipeline/ai-extract.ts`` (2026-08-23), with two
hard-won lessons baked in:

- **Single call, no tool loop.** iris §1e: multi-step tool rounds on DeepSeek
  thinking models drop ``reasoning_content`` when the SDK re-encodes assistant
  messages, and DeepSeek then rejects the request with 400. One
  chat-completions call with the reduced page inline avoids that entire
  failure class, so this port has no tools and no multi-step path.
- **503 is transient.** Zen's DeepSeek free tier intermittently returns 503
  "Service Unavailable" under load even when the identical request succeeds a
  moment later (confirmed live 2026-08-19: a Kogan extraction that 503'd on
  the scheduler tick returned 200 with a correct price when replayed
  manually). Treating 503 as terminal would roll back product creates for a
  transient outage, so 429/502/503/504 retry with exponential backoff;
  everything else is terminal.

Throttle parity with iris: a process-wide concurrency semaphore captured once
at first use (boot-time, mirroring iris's memoized ``pLimit``) plus a
min-interval clock read live on every call. This module-level state is the
**one documented exception** to the no-module-global-lifecycle-state rule
(quality-guidelines.md); ``_reset_throttle()`` exists for tests only.

Secret handling (logging-guidelines.md): the API key authenticates every
request but is NEVER logged — log lines carry url/model/error shapes only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from random import random
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from .config import Settings

logger = logging.getLogger("argus.ai")

# LLM generation latency is far above httpx's 5s default timeout — free-tier
# models can legitimately think for a minute on a long reduced page.
_REQUEST_TIMEOUT_SECONDS = 120.0

# Transient upstream failures worth a backoff retry (see module docstring for
# the 2026-08-19 503 incident). Everything else is terminal on first sight.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

# reduce_html caps, ported verbatim from iris ``reducePageHtml``: keep the
# model context small while still feeding it client-side-rendered prices.
_VISIBLE_TEXT_CAP = 8_000
_BLOB_CAP = 3_000
_BLOB_MAX = 3

_SCRIPT_RE = re.compile(r"<script[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
# Blobs worth embedding: the JSON payloads React/Next shops hydrate prices
# into (iris's filter list, unchanged).
_PRICE_BLOB_RE = re.compile(r"price|formattedValue|currency|amount|offers", re.IGNORECASE)


def reduce_html(html: str) -> str:
    """Reduce a raw page to what a price-extracting model needs to read.

    Two labeled sections (exactly iris's shape, so prompts stay comparable
    across services): the visible text (scripts/styles/comments/tags stripped,
    whitespace collapsed, capped at 8 KB) plus up to 3 ``<script>`` blobs
    matching price-ish keys, each capped at 3 KB — client-side-rendered shops
    put the price in those blobs and nowhere in the visible text.
    """
    blobs: list[str] = []
    for match in _SCRIPT_RE.finditer(html):
        blob = match.group(1)
        if _PRICE_BLOB_RE.search(blob):
            blobs.append(blob[:_BLOB_CAP])
            if len(blobs) == _BLOB_MAX:
                break

    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()

    parts = [f"VISIBLE TEXT:\n{text[:_VISIBLE_TEXT_CAP]}"]
    if blobs:
        parts.append("EMBEDDED PRICE DATA:\n" + "\n---\n".join(blobs))
    return "\n\n".join(parts)


def _build_prompt(url: str, page_content: str) -> str:
    """Single-call extraction prompt (port of iris's page-content prompt)."""
    return f"""
Product URL: {url}

The product page has already been fetched. Below is a compact representation of
its content (visible text plus any embedded price data).

Extract the current selling price of the single product on this page, its
currency, and its name. If the page shows the product as out of stock, or no
price is visible anywhere in the page content, set "available" to false and
use null for the fields you could not determine.

Return ONLY a single JSON object — no prose, no markdown — exactly matching
one of these shapes:
{{"price": 119, "currency": "NZD", "name": "Product name", "available": true}}
{{"price": null, "currency": null, "name": null, "available": false}}

PAGE CONTENT:
{page_content}
"""


class _AvailableExtraction(BaseModel):
    """Model said purchasable: a positive price and currency are mandatory."""

    available: Literal[True]
    price: float = Field(gt=0)
    currency: str = Field(min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1)


class _UnavailableExtraction(BaseModel):
    """Model said out of stock / no price visible: fields may be null."""

    available: Literal[False]
    price: float | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1)


# Discriminated on ``available`` (mirrors iris's priceExtractionSchema): the
# unavailable branch must accept nulls or honest "I couldn't find it" model
# responses would fail validation.
_ParsedExtraction = Annotated[
    _AvailableExtraction | _UnavailableExtraction,
    Field(discriminator="available"),
]
_EXTRACTION_ADAPTER: TypeAdapter[_ParsedExtraction] = TypeAdapter(_ParsedExtraction)


@dataclass
class PriceExtraction:
    """Caller-facing result of one AI extraction.

    ``price`` is a Decimal-normalized 2dp string (e.g. ``"119.00"``) — same
    contract as the JSON-LD path, never a float.
    """

    available: bool
    price: str | None
    currency: str | None
    name: str | None


def _parse_extraction(text: str) -> _ParsedExtraction:
    """Locate and schema-validate the JSON object in a model response.

    Tolerates prose/markdown fences around the object (slice first ``{`` to
    last ``}``). Raises ValueError/JSONDecodeError/ValidationError on garbage —
    terminal, never retried: a second identical call to the same model is
    unlikely to fix a formatting problem.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    data = json.loads(text[start : end + 1])
    return _EXTRACTION_ADAPTER.validate_python(data)


def _to_result(parsed: _ParsedExtraction) -> PriceExtraction:
    """Validated model output → caller-facing dataclass (2dp price string)."""
    if parsed.available:
        price = Decimal(str(parsed.price)).quantize(Decimal("0.01"))
        return PriceExtraction(
            available=True,
            price=str(price),
            currency=parsed.currency,
            name=parsed.name,
        )
    return PriceExtraction(available=False, price=None, currency=None, name=None)


class _ProviderStatusError(Exception):
    """HTTP-level provider error; carries the status for retry decisions."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"provider returned HTTP {status}: {detail}")
        self.status = status


def _is_retryable(exc: BaseException) -> bool:
    """Whether one failed attempt is worth a backoff retry.

    Status-carrying errors decide on the status code (429/502/503/504 only).
    Everything else falls back to iris's message heuristics — network-layer
    errors sometimes carry gateway wording without a parsed status.
    """
    if isinstance(exc, _ProviderStatusError):
        return exc.status in _RETRYABLE_STATUSES
    message = str(exc)
    if re.search(r"rate limit", message, re.IGNORECASE):
        return True
    return (
        re.search(
            r"\b(502|503|504)\b|service unavailable|bad gateway|gateway timeout",
            message,
            re.IGNORECASE,
        )
        is not None
    )


# --- throttle (the documented module-global exception, mirrors iris) ----------

# Concurrency is captured once at first use — boot-time configuration, exactly
# like iris's memoized pLimit. The min-interval gap is read live per call, so
# ARGUS_AI_MIN_INTERVAL_MS is tunable without a restart.
_throttle_semaphore: asyncio.Semaphore | None = None
_last_call_ended_at = 0.0


def _get_throttle_semaphore(concurrency: int) -> asyncio.Semaphore:
    global _throttle_semaphore
    if _throttle_semaphore is None:
        _throttle_semaphore = asyncio.Semaphore(concurrency)
    return _throttle_semaphore


def _mark_call_ended() -> None:
    global _last_call_ended_at
    _last_call_ended_at = time.monotonic()


async def _wait_for_min_interval(min_interval_ms: int) -> None:
    elapsed_ms = (time.monotonic() - _last_call_ended_at) * 1000
    remaining_ms = min_interval_ms - elapsed_ms
    if remaining_ms > 0:
        await _sleep(remaining_ms / 1000)


async def _sleep(seconds: float) -> None:
    """``asyncio.sleep`` indirection so tests can fast-forward backoff waits."""
    await asyncio.sleep(seconds)


def _reset_throttle() -> None:
    """Reset the module-level throttle state. For tests only: clears the
    memoized semaphore (so a new concurrency takes effect) and the
    min-interval clock. Production never calls this.

    """
    global _throttle_semaphore, _last_call_ended_at
    _throttle_semaphore = None
    _last_call_ended_at = 0.0


# --- client --------------------------------------------------------------------


class AiClient:
    """OpenAI-compatible chat-completions client for price extraction.

    Constructed once in ``main.lifespan`` and stashed on ``app.state`` (same
    singleton pattern as ``BrowserManager``); ``build_ai_client`` returns None
    when unconfigured so the route degrades to a logged no-op instead of 500.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def extract_price(self, html: str, url: str) -> PriceExtraction | None:
        """Extract a price from already-fetched page HTML. Never throws.

        Every failure (provider error after retries, malformed response,
        schema mismatch) is logged — without any credential material — and
        maps to None, which the route renders as ``extraction_failed``.
        """
        try:
            semaphore = _get_throttle_semaphore(self._settings.ai_concurrency)
            async with semaphore:
                return await self._extract_with_retry(html, url)
        except Exception as exc:  # noqa: BLE001 — never throw to the caller
            logger.error(
                "AI price extraction failed url=%s model=%s error=%s",
                url,
                self._settings.ai_model,
                exc,
            )
            return None

    async def _extract_with_retry(self, html: str, url: str) -> PriceExtraction:
        prompt = _build_prompt(url, reduce_html(html))
        max_retries = self._settings.ai_max_retries
        for attempt in range(1, max_retries + 1):
            # Min-interval gap before EVERY attempt; read live so operators
            # can widen the gap without a restart (iris parity).
            await _wait_for_min_interval(self._settings.ai_min_interval_ms)
            try:
                content = await self._complete(prompt)
            except Exception as exc:
                _mark_call_ended()
                if _is_retryable(exc) and attempt < max_retries:
                    # Exponential backoff with jitter, iris's formula: the
                    # free-tier 429 quota burst needs spreading, not hammering.
                    delay_ms = 2**attempt * 1000 + random() * 1000
                    logger.warning(
                        "transient AI provider error, retrying url=%s "
                        "attempt=%d delay_ms=%d error=%s",
                        url,
                        attempt,
                        round(delay_ms),
                        exc,
                    )
                    await _sleep(delay_ms / 1000)
                    continue
                raise
            _mark_call_ended()
            return _to_result(_parse_extraction(content))
        # Unreachable with max_retries >= 1 (the loop always returns or
        # raises); kept for exhaustiveness, mirrors iris's trailing throw.
        raise ValueError(f"AI extraction failed after {max_retries} attempts")

    async def _complete(self, prompt: str) -> str:
        """One chat-completions round trip; returns the message content."""
        endpoint = f"{self._settings.ai_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.ai_api_key}"}
        headers.update(self._zen_headers())
        payload = {
            "model": self._settings.ai_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        response = await self._client.post(endpoint, json=payload, headers=headers)
        if response.status_code >= 400:
            raise _ProviderStatusError(response.status_code, response.text[:200])
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"malformed completion response: {exc}") from exc
        if not isinstance(content, str):
            raise ValueError("completion response content is not a string")
        return content

    def _zen_headers(self) -> dict[str, str]:
        """Optional Zen parity headers (iris ``createModel`` parity).

        When the endpoint host matches ``ai_zen_host``, send the configured
        ``User-Agent`` / ``X-Opencode-Client`` so argus and iris can share one
        free-tier provider's client allowlist.
        """
        headers: dict[str, str] = {}
        zen_host = self._settings.ai_zen_host
        if zen_host and urlparse(self._settings.ai_base_url).hostname == zen_host:
            if self._settings.ai_user_agent:
                headers["User-Agent"] = self._settings.ai_user_agent
            if self._settings.ai_client_header:
                headers["X-Opencode-Client"] = self._settings.ai_client_header
        return headers

    async def aclose(self) -> None:
        """Close the underlying HTTP client (lifespan shutdown)."""
        await self._client.aclose()


def build_ai_client(settings: Settings) -> AiClient | None:
    """The production AiClient, or None when the AI stage is disabled.

    All three of base URL / API key / model must be non-empty; leaving any
    empty makes /v1/extract-price a JSON-LD-only service (AC4: a missing key
    degrades to a logged no-op at the route, never a 500).
    """
    if not (settings.ai_base_url and settings.ai_api_key and settings.ai_model):
        return None
    return AiClient(settings)
