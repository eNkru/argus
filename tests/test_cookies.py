"""Cookie normalization to the Playwright add_cookies dict shape."""

from __future__ import annotations

from argus.cookies import to_playwright_cookies
from argus.schemas import Cookie


def test_to_playwright_cookies_shape() -> None:
    cookies = [
        Cookie(
            name="login5",
            value="abc",
            domain=".taobao.com",
            path="/",
            httpOnly=True,
            secure=True,
            sameSite="Lax",
        ),
        Cookie(name="t", value="v", domain=".tmall.com"),
    ]
    result = to_playwright_cookies(cookies)
    assert result == [
        {
            "name": "login5",
            "value": "abc",
            "domain": ".taobao.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "t",
            "value": "v",
            "domain": ".tmall.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]


def test_keys_match_playwright_add_cookies_contract() -> None:
    # Playwright's add_cookies accepts exactly these keys.
    [item] = to_playwright_cookies(
        [Cookie(name="n", value="v", domain=".example.com")]
    )
    assert set(item.keys()) == {
        "name",
        "value",
        "domain",
        "path",
        "httpOnly",
        "secure",
        "sameSite",
    }
