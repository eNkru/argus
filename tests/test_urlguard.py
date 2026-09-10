"""``urlguard`` — pure SSRF host classifier + escape-hatch entry point.

No network, no DNS, no camoufox import. Covers the literal-host blocklist:
IP-literal private/loopback/link-local/reserved/multicast/unspecified
(incl. IPv4-mapped IPv6) and the well-known cloud-metadata hostnames.
"""

from __future__ import annotations

import pytest

from argus.config import Settings
from argus.urlguard import guard_url, is_private_target


def _settings(allow_private: bool) -> Settings:
    return Settings.model_construct(fetch_allow_private=allow_private)


# --- is_private_target: blocked ------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        # cloud metadata — the headline SSRF target
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/",
        "https://metadata.azure.com/",
        # loopback
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://localhost:8000/",
        "http://[::1]/",
        # RFC1918
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.0.1/",
        # link-local
        "http://169.254.10.20/",
        "http://[fe80::1]/",
        # this-network / unspecified / reserved / broadcast
        "http://0.0.0.0/",
        "http://255.255.255.255/",
        # IPv4-mapped IPv6 private
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_is_private_target_blocks_private(url: str) -> None:
    assert is_private_target(url) is True


# --- is_private_target: allowed ------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://pbtech.co.nz/product/abc",
        "http://8.8.8.8/",          # public DNS
        "https://1.1.1.1/",          # public DNS
        "http://example.test/pdp",  # the existing test fixture host
    ],
)
def test_is_private_target_allows_public(url: str) -> None:
    assert is_private_target(url) is False


def test_is_private_target_none_host_is_not_private() -> None:
    # No host — not the guard's concern (scheme validator / browser handle it).
    assert is_private_target("http:///path") is False


# --- guard_url: the route-facing entry point -----------------------------------

def test_guard_url_blocks_private_when_disallowed() -> None:
    assert guard_url("http://169.254.169.254/", _settings(False)) is True


def test_guard_url_allows_public_when_disallowed() -> None:
    assert guard_url("https://example.com/", _settings(False)) is False


def test_guard_url_escape_hatch_allows_private() -> None:
    # ARGUS_FETCH_ALLOW_PRIVATE=true — exact prior behavior, guard is a no-op.
    assert guard_url("http://169.254.169.254/", _settings(True)) is False


def test_guard_url_blocks_localhost() -> None:
    assert guard_url("http://localhost/", _settings(False)) is True
