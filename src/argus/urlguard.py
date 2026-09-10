"""SSRF fetch-target guard — block private/loopback/metadata URLs by default.

Every fetch route (``/v1/fetch``, ``/v1/extract-price``, ``/v1/fetch-image``)
navigates the browser to a caller-supplied ``http(s)://`` URL. Without a host
filter, an authenticated caller (or any caller when ``ARGUS_AUTH_DISABLED``)
could drive the browser at ``http://169.254.169.254/...`` (cloud IAM metadata),
``127.0.0.1``, RFC1918 ranges, or ``localhost`` — a JS-executing SSRF proxy
that returns the rendered HTML / base64 body. Worse than plain HTTP SSRF
because it carries a real browser fingerprint and runs page scripts.

This module is the literal-host blocklist. It is **pure** (no network, no DNS):
it inspects the URL's declared host only. ``is_private_target`` classifies a
host; ``guard_url`` is the route-facing entry point that honors the operator
escape hatch (``Settings.fetch_allow_private``) and logs the block.

Why literal-host only (no DNS rebinding): resolving a hostname to an IP at
fetch time would add a network call to the hot path and break the
"no network in tests" invariant for the guard module. The literal-host guard
closes the highest-severity hole (IP literals + known metadata hostnames)
without that cost; DNS-rebinding hardening is deferred (see the
``09-09-ssrf-blocklist`` design notes).

A blocked target maps to ``{ok:false, reason:"fetch_failed"}`` in every route
(zero response-contract change — the fetch path's "never throw" contract in
``error-handling.md`` is preserved). The block is **not** recorded on the
``FailureTracker``: a policy block is neither a fetch success nor a fetch
failure, and counting it would misattribute "browser degraded" after three
SSRF probes (``diagnostics.py`` pattern 4 — blocked ≠ failed).
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

from .config import Settings

logger = logging.getLogger("argus.urlguard")

# Hostnames (not IP literals) that resolve to private/internal targets and are
# the well-known cloud-metadata endpoints. urlparse already lowercases the
# host; the ``.rstrip(".")`` defends against a trailing dot some clients send.
_METADATA_HOSTS = frozenset(
    {
        "localhost",
        "metadata",  # AWS-style short name
        "metadata.google.internal",  # GCP
        "metadata.azure.com",  # Azure
    }
)


def _host(url: str) -> str | None:
    """The lowercased URL hostname (no port, no brackets), or None."""
    host = urlparse(url).hostname
    if host is None:
        return None
    return host.rstrip(".").lower()


def _is_private_ip_literal(host: str) -> bool:
    """Whether an IP-literal host is a private/internal/loopback target.

    Uses the ``ipaddress`` built-in classification properties rather than a
    hand-maintained net list — they cover RFC 6890 (loopback, link-local,
    RFC1918, this-network, CGNAT, reserved, multicast, unspecified) and stay
    correct as registries evolve. IPv4-mapped IPv6 (``::ffff:10.0.0.1``) is
    checked against its mapped v4 too, since the IPv6 properties don't see
    through the mapping.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP literal — hostname path handles it

    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
    ):
        return True

    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and (
        mapped.is_loopback
        or mapped.is_link_local
        or mapped.is_private
        or mapped.is_unspecified
        or mapped.is_reserved
        or mapped.is_multicast
    ):
        return True

    return False


def is_private_target(url: str) -> bool:
    """Whether a fetch URL targets a private/internal/loopback/metadata host.

    Pure: no network, no DNS. Operates on the URL's declared host only.
    Returns ``False`` for URLs without a host (the scheme validator or the
    browser's own navigation handles those; a non-private ``False`` here just
    means "not blocked by this guard").
    """
    host = _host(url)
    if host is None:
        return False
    if _is_private_ip_literal(host):
        return True
    return host in _METADATA_HOSTS


def guard_url(url: str, settings: Settings) -> bool:
    """Block a fetch URL under the SSRF policy. Returns ``True`` if blocked.

    Honors ``settings.fetch_allow_private``: when ``True`` (operator escape
    hatch for deployments that legitimately scrape internal hosts) the guard
    is a no-op and returns ``False`` for every URL — exact prior behavior.

    When blocking, logs at ``warning`` with the URL and resolved host (both
    request inputs, safe to log per ``logging-guidelines.md`` rule 3). Does
    NOT touch the ``FailureTracker`` (see module docstring).
    """
    if settings.fetch_allow_private:
        return False
    if not is_private_target(url):
        return False
    logger.warning("ssrf block url=%s host=%s", url, _host(url))
    return True
