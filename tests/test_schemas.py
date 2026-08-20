"""Request model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.schemas import Cookie, FetchImageRequest, FetchRequest


def test_fetch_request_defaults() -> None:
    req = FetchRequest(url="https://example.com")
    assert req.cookies is None
    assert req.waitUntil == "domcontentloaded"
    assert req.renderWaitMs == 8000
    assert req.timeoutMs == 35000
    assert req.detectBlocked is True
    assert req.locale is None
    assert req.userAgent is None


def test_fetch_request_rejects_relative_url() -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url="example.com/page")


def test_fetch_request_rejects_non_http_url() -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url="ftp://example.com")


def test_fetch_image_request_rejects_relative_url() -> None:
    with pytest.raises(ValidationError):
        FetchImageRequest(url="/image.png")


def test_cookie_defaults() -> None:
    cookie = Cookie(name="n", value="v", domain=".example.com")
    assert cookie.path == "/"
    assert cookie.httpOnly is False
    assert cookie.secure is False
    assert cookie.sameSite == "Lax"


def test_cookie_sameSite_validation() -> None:
    with pytest.raises(ValidationError):
        Cookie(name="n", value="v", domain=".example.com", sameSite="Bogus")


def test_cookie_requires_domain_and_path_explicit() -> None:
    # domain is required; path defaults. value/name required.
    with pytest.raises(ValidationError):
        Cookie(name="n", value="v")  # type: ignore[call-arg]
