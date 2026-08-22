"""Pydantic request/response models for the public API.

The ``Cookie`` model mirrors the object a browser devtools "copy all cookies"
produces (and the shape ``BrowserContext.add_cookies`` accepts), so a Taobao
cookie jar can be pasted verbatim. ``httpOnly`` / ``secure`` / ``sameSite`` are
optional with sensible defaults; ``name`` / ``value`` / ``domain`` / ``path``
are required (a per-cookie domain is needed because login cookies span multiple
subdomains — e.g. ``.taobao.com`` and ``.tmall.com``).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, field_validator


class Cookie(BaseModel):
    name: str
    value: str
    domain: str
    path: str = "/"
    httpOnly: bool = False
    secure: bool = False
    sameSite: Literal["Strict", "Lax", "None"] = "Lax"


class FetchRequest(BaseModel):
    url: str
    # Optional. Omit for login-free sites; pass the logged-in cookie jar for
    # login-gated sites (e.g. Taobao).
    cookies: Optional[list[Cookie]] = None
    waitUntil: Literal["domcontentloaded", "load", "networkidle"] = (
        "domcontentloaded"
    )
    renderWaitMs: int = 8000
    timeoutMs: int = 35000
    detectBlocked: bool = True
    # Optional context overrides. See architecture.md for the userAgent
    # fingerprint caveat.
    locale: Optional[str] = None
    userAgent: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_must_be_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http(s) URL")
        return value


class FetchResponseOk(BaseModel):
    ok: bool = True
    html: str
    url: str


class FetchResponseFail(BaseModel):
    ok: bool = False
    reason: Literal["blocked", "fetch_failed"]
    signature: Optional[str] = None
    # Present only when reason="blocked". Argus owns the signature registry, so
    # it surfaces the retryable verdict so callers can decide whether to retry
    # without reimplementing the registry.
    retryable: Optional[bool] = None


class ExtractPriceRequest(BaseModel):
    """Same navigation knobs as ``FetchRequest`` — extraction fetches the page
    itself — plus ``aiFallback`` to opt out of the LLM stage (deterministic
    JSON-LD-only mode)."""

    url: str
    cookies: Optional[list[Cookie]] = None
    waitUntil: Literal["domcontentloaded", "load", "networkidle"] = (
        "domcontentloaded"
    )
    renderWaitMs: int = 8000
    timeoutMs: int = 35000
    detectBlocked: bool = True
    locale: Optional[str] = None
    userAgent: Optional[str] = None
    # Default true = the caller asked for extraction (JSON-LD → AI). False
    # guarantees no LLM cost/latency: a JSON-LD miss maps straight to
    # {ok:false, reason:"extraction_failed"}.
    aiFallback: bool = True

    @field_validator("url")
    @classmethod
    def _url_must_be_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http(s) URL")
        return value


class ExtractPriceResponseOk(BaseModel):
    ok: bool = True
    # "jsonld" = deterministic parse of the page's structured data;
    # "ai" = LLM fallback (jsonld is always None on that path).
    source: Literal["jsonld", "ai"]
    url: str
    available: bool
    # Decimal-normalized 2dp string (e.g. "599.99") — never a float, so the
    # value round-trips JSON without binary rounding.
    price: Optional[str] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    name: Optional[str] = None
    # The primary schema.org Product node (source="jsonld" only) — the rich
    # structured data (name/brand/gtin/reviews ride along); null for source="ai"
    # and for unavailable products with no node to return.
    jsonld: Optional[dict] = None


class ExtractPriceResponseFail(BaseModel):
    ok: bool = False
    # "extraction_failed": page fetched fine but no price could be extracted
    # (JSON-LD miss with aiFallback=false or unconfigured/degraded AI).
    reason: Literal["blocked", "fetch_failed", "extraction_failed"]
    signature: Optional[str] = None
    retryable: Optional[bool] = None


class FetchImageRequest(BaseModel):
    url: str
    cookies: Optional[list[Cookie]] = None
    timeoutMs: int = 35000
    locale: Optional[str] = None
    userAgent: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_must_be_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http(s) URL")
        return value


class FetchImageResponseOk(BaseModel):
    ok: bool = True
    contentType: str
    data: str  # base64-encoded binary image data


class FetchImageResponseFail(BaseModel):
    ok: bool = False
    reason: Literal["fetch_failed", "non_image"]
