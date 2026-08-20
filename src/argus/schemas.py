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
