"""Anti-bot WAF signature registry for fetched HTML.

Ported from iris's ``packages/prices/src/pipeline/blocked-signatures.ts``. The
predicates, ordering, and retryable flags are preserved verbatim because they
encode live-proven behavior (Akamai Bot Manager on farmers.co.nz, Cloudflare
managed challenges on kogan/pbtech, DataDome) — see the per-signature notes.

When a WAF blocks a fetch, the browser still returns 200 OK with HTML — it's
just the *wrong* HTML (a captcha / challenge / deny page). This registry
classifies such pages so the caller gets an explicit
``{ok:false, reason:"blocked", signature:"...", retryable:bool}`` instead of
garbage HTML masquerading as content.

Intentionally a generic registry (id + predicate), NOT per-retailer code: the
same signatures apply to every retailer. The registry is a plain list
(``DEFAULT_REGISTRY``) so signatures can be extended without touching the fetch
path; per-request ``detectBlocked: false`` opts out entirely (the raw HTML is
returned as ``{ok:true, html}`` for a caller that wants to classify itself).
"""

from __future__ import annotations

import re
from typing import Callable, Optional


class BlockedSignature:
    def __init__(
        self, id: str, test: Callable[[str], bool], retryable: bool = True
    ) -> None:
        self.id = id
        self.test = test
        # Whether a fresh fetch attempt can plausibly pass this block. Challenge
        # shells (behavioral challenges, captchas, managed challenges) are
        # probabilistic — a new page often passes where the previous one failed
        # (confirmed live on farmers.co.nz 2026-08-08: the Akamai behavioral
        # challenge passes roughly half of fresh attempts). Final deny pages
        # (WAF deny) are fingerprint-level verdicts; retrying just burns
        # latency. Unknown/new ids default to retryable (True) so new challenge
        # shapes are never written off without another attempt.
        self.retryable = retryable


# --- compiled predicates (detect runs on every fetched HTML) ----------------

_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title[\s>]", re.IGNORECASE)
_BODY_TAG_RE = re.compile(r"<body[\s>]", re.IGNORECASE)
_ACCESS_DENIED_RE = re.compile(r"access\s*denied", re.IGNORECASE)
_ADD_TO_BAG_RE = re.compile(r"add to (bag|cart)", re.IGNORECASE)
_JUST_A_MOMENT_RE = re.compile(r"just a moment", re.IGNORECASE)


def _title(html: str) -> str:
    """The page <title> text, or "" when absent."""
    m = _TITLE_RE.search(html)
    return m.group(1) if m else ""


# Known deny-page signatures. Order matters: the most generic predicate
# (akamai-empty-shell) is LAST so it never shadows a more specific signature
# above it.
DEFAULT_REGISTRY: list[BlockedSignature] = [
    # Akamai Bot Manager — final deny verdict. A fresh attempt is futile (same
    # fingerprint).
    BlockedSignature(
        id="akamai-waf",
        retryable=False,
        test=lambda html: "/WAF_Deny_Page/" in html,
    ),
    # Akamai edge-level "Access Denied" after a soft pass. Live behavior on
    # farmers.co.nz (2026-08-08: one PDP fetched fine ~3 min after another PDP
    # served this exact shell) shows the deny is intermittent — Akamai
    # re-evaluates the behavioral signal per request and a fresh attempt often
    # passes. Keep the length cap so a real product page that mentions the
    # phrase in body copy is not false-positive.
    BlockedSignature(
        id="akamai-access-denied",
        retryable=True,
        test=lambda html: (
            len(html) < 5_000 and bool(_ACCESS_DENIED_RE.search(_title(html)))
        ),
    ),
    # Akamai behavioral challenge — probabilistic; a fresh page often passes.
    # Real PDPs that embed Akamai scripts still have cart/price chrome, so the
    # "add to bag/cart" check guards against false positives.
    BlockedSignature(
        id="akamai-behavioral-challenge",
        retryable=True,
        test=lambda html: (
            "sec-if-cpt-container" in html
            and len(html) < 20_000
            and not _ADD_TO_BAG_RE.search(html)
        ),
    ),
    # DataDome captcha — served when the anti-bot challenge is not solved.
    BlockedSignature(
        id="datadome-captcha",
        test=lambda html: "captcha-delivery.com" in html,
    ),
    # Cloudflare managed challenge interstitial ("Just a moment…"). Match only
    # challenge-shell markers — NOT every Cloudflare asset. Real product pages
    # (confirmed pbtech PDP 2026-08-04) embed a Turnstile widget that loads
    # challenges.cloudflare.com while still being a full PDP with price +
    # add-to-cart. Treating bare challenges.cloudflare.com as a block was a
    # false positive that rolled create back. Managed-challenge shells always
    # inject _cf_chl_opt / cf-chl tokens, or are a tiny "Just a moment…" page.
    # Only treat the challenges host as a block on small pages (interstitial
    # size), never on multi-hundred-KB PDPs.
    BlockedSignature(
        id="cloudflare-challenge",
        test=lambda html: (
            "_cf_chl_opt" in html
            or "cf-chl" in html
            or (
                len(html) < 5_000
                and (
                    bool(_JUST_A_MOMENT_RE.search(_title(html)))
                    or "challenges.cloudflare.com" in html
                )
            )
        ),
    ),
    # Head-only empty shell — LAST on purpose: it is the most generic predicate
    # (any tiny page with no <title> and no <body>). A real page always has a
    # <title> and a <body>; challenge/deny shells observed live on farmers.co.nz
    # 2026-08-08 have neither. Retryable: it is the failed-challenge snapshot,
    # and a fresh attempt often passes.
    BlockedSignature(
        id="akamai-empty-shell",
        retryable=True,
        test=lambda html: (
            len(html) < 5_000
            and not _TITLE_TAG_RE.search(html)
            and not _BODY_TAG_RE.search(html)
        ),
    ),
]


def detect(
    html: str,
    *,
    enabled: bool,
    registry: list[BlockedSignature] | None = None,
) -> Optional[str]:
    """Return the id of the first matched signature, or None.

    No-op when ``enabled`` is False (the caller opted out of classification).
    """
    if not enabled:
        return None
    reg = registry if registry is not None else DEFAULT_REGISTRY
    for sig in reg:
        if sig.test(html):
            return sig.id
    return None


def is_retryable(signature_id: str) -> bool:
    """Whether a blocked result with the given signature id is worth retrying.

    Unknown ids default to retryable (True) so new challenge shapes are never
    written off without another attempt.
    """
    for sig in DEFAULT_REGISTRY:
        if sig.id == signature_id:
            return sig.retryable
    return True
