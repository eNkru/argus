"""Blocked-signature registry detection (ported from iris)."""

from __future__ import annotations

from argus.signatures import DEFAULT_REGISTRY, detect, is_retryable


# --- opt-out + no-match -----------------------------------------------------

def test_detect_disabled_returns_none() -> None:
    assert detect("<html>cloudflare</html>", enabled=False) is None


def test_detect_real_page_returns_none() -> None:
    # A realistic, content-bearing page is well above every length cap and
    # carries none of the challenge markers.
    html = "<html><head><title>Real Product</title></head><body><h1>Real Product</h1>" + (
        "<p>content here</p>" * 30
    ) + "</body></html>"
    assert detect(html, enabled=True) is None


# --- akamai-waf --------------------------------------------------------------

def test_detect_akamai_waf() -> None:
    assert detect('<a href="/WAF_Deny_Page/x">denied</a>', enabled=True) == "akamai-waf"


def test_akamai_waf_not_retryable() -> None:
    assert is_retryable("akamai-waf") is False


# --- akamai-access-denied ----------------------------------------------------

def test_detect_akamai_access_denied_small_page() -> None:
    html = "<html><head><TITLE>Access Denied</TITLE></head><body>x</body></html>"
    assert detect(html, enabled=True) == "akamai-access-denied"


def test_akamai_access_denied_skipped_on_large_page() -> None:
    # Length cap so a real product page that mentions the phrase in body copy
    # is not false-positive.
    html = "<html><head><title>Access denied</title></head><body>" + ("x" * 6_000) + "</body></html>"
    assert detect(html, enabled=True) is None


def test_akamai_access_denied_retryable() -> None:
    assert is_retryable("akamai-access-denied") is True


# --- akamai-behavioral-challenge --------------------------------------------

def test_detect_akamai_behavioral_challenge() -> None:
    html = '<html><body><div class="sec-if-cpt-container"></div></body></html>'
    assert detect(html, enabled=True) == "akamai-behavioral-challenge"


def test_akamai_behavioral_challenge_skipped_when_real_pdp_chrome_present() -> None:
    # A real PDP that embeds Akamai scripts still has cart/price chrome.
    html = (
        '<html><body><div class="sec-if-cpt-container"></div>'
        "<button>Add to cart</button></body></html>"
    )
    assert detect(html, enabled=True) is None


# --- datadome-captcha --------------------------------------------------------

def test_detect_datadome_captcha() -> None:
    html = '<script src="https://ct.captcha-delivery.com/c.js"></script>'
    assert detect(html, enabled=True) == "datadome-captcha"


# --- cloudflare-challenge ---------------------------------------------------

def test_detect_cloudflare_challenge_via_cf_chl_opt() -> None:
    html = "<html><body><script>window._cf_chl_opt = {};</script></body></html>"
    assert detect(html, enabled=True) == "cloudflare-challenge"


def test_detect_cloudflare_challenge_just_a_moment_small_page() -> None:
    html = "<html><head><title>Just a moment...</title></head></html>"
    assert detect(html, enabled=True) == "cloudflare-challenge"


def test_cloudflare_challenges_host_not_blocked_on_large_page() -> None:
    # A real PDP embeds a Turnstile widget loading challenges.cloudflare.com;
    # it must NOT be treated as a block (regression guard).
    html = (
        "<html><head><title>Real PDP</title></head><body>"
        "<script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js\"></script>"
        + ("<p>price content</p>" * 300)
        + "</body></html>"
    )
    assert detect(html, enabled=True) is None


# --- akamai-empty-shell (most generic, must be last) -------------------------

def test_detect_empty_shell_tiny_no_title_no_body() -> None:
    assert detect("   ", enabled=True) == "akamai-empty-shell"
    assert detect("x" * 150, enabled=True) == "akamai-empty-shell"


def test_empty_shell_not_matched_when_body_present() -> None:
    # Has a <body> tag → not an empty shell, even if tiny.
    html = "<html><body>short but real</body></html>"
    assert detect(html, enabled=True) is None


def test_order_more_specific_signature_wins_over_empty_shell() -> None:
    # A tiny page with no title/no body that ALSO carries the WAF deny marker
    # must classify as akamai-waf (earlier in the registry), not empty-shell.
    html = "<a href='/WAF_Deny_Page/'>no title no body</a>"
    assert detect(html, enabled=True) == "akamai-waf"


# --- registry plumbing ------------------------------------------------------

def test_custom_registry_used_when_supplied() -> None:
    from argus.signatures import BlockedSignature

    custom = [BlockedSignature(id="custom-sig", test=lambda html: "WIDGET" in html)]
    assert detect("<body>WIDGET</body>", enabled=True, registry=custom) == "custom-sig"
    assert detect("<body>cloudflare</body>", enabled=True, registry=custom) is None


def test_default_registry_empty_shell_is_last() -> None:
    # Ordering invariant: the most generic predicate must come last.
    assert DEFAULT_REGISTRY[-1].id == "akamai-empty-shell"


def test_unknown_signature_defaults_retryable() -> None:
    assert is_retryable("never-seen-before") is True
