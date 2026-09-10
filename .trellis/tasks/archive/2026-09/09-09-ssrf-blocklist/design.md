# Design — SSRF fetch-target blocklist

> Why these choices, referencing real files/lines. See `prd.md` for requirements.

## Where the guard lives

The three fetch routes:

- `POST /v1/fetch` (`routes/fetch.py`) → `_do_fetch` → `navigate.fetch_html`
- `POST /v1/extract-price` (`routes/extract.py`) → `_do_extract` → `navigate.fetch_html`
- `POST /v1/fetch-image` (`routes/fetch_image.py`) → `_do_fetch_image` (own inline Playwright lifecycle; does NOT call `navigate.fetch_html` — see its module docstring)

So `navigate.fetch_html` is the shared seam for two of the three; `fetch_image`
is separate. The guard must cover all three. A new pure module
`src/argus/urlguard.py` holds the logic; both call sites call it. This mirrors
the codebase's existing precedent of extracting shared helpers into a module
(`navigate.py` was extracted from `routes/fetch.py` for exactly this reason —
"~70 lines of lifecycle-critical cleanup across two routes was the bug waiting
to happen").

## Module surface

```python
# src/argus/urlguard.py
from __future__ import annotations
import ipaddress
import logging
from urllib.parse import urlparse
from .config import Settings
from .diagnostics import FailureTracker  # structural type only

logger = logging.getLogger("argus.urlguard")

_PRIVATE_V4_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),      # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),   # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
)
_PRIVATE_V6_NETS = (
    ipaddress.ip_network("::1/128"),         # v6 loopback
    ipaddress.ip_network("fe80::/10"),       # v6 link-local
)
_METADATA_HOSTS = frozenset({
    "localhost", "metadata.google.internal",
    "metadata", "metadata.azure.com",
})

def is_private_target(url: str) -> bool: ...
def guard_url(url: str, settings: Settings, tracker) -> "HtmlFailed | None": ...
```

`is_private_target` parses with `urlparse`, takes `.hostname`, lowercases,
strips a trailing dot, and:
1. If `hostname` is `None` → `False` (let the existing scheme/validator path
   handle malformed input).
2. Try `ipaddress.ip_address(hostname)`; on success, test membership in the
   private nets (for IPv4-mapped IPv6, `ipaddress` exposes `.ipv4_mapped`).
3. Else test `hostname in _METADATA_HOSTS`.

`guard_url` returns `HtmlFailed()` (the existing union member — see
`navigate.py`) when `not settings.fetch_allow_private and
is_private_target(url)`, after `tracker.record_failure(url, None,
kind="blocked_url")` and an `INFO` log (`url=%s` only). Returns `None`
otherwise. Returning `HtmlFailed` lets both routes' existing `_do_fetch` /
`_do_extract` matchers map it straight to `FetchResponseFail(reason="fetch_failed")`
/ `ExtractPriceResponseFail(reason="fetch_failed")` with **zero response-contract
change** — exactly the R4 constraint.

## Call-site edits

`navigate.fetch_html` — first lines of the function, before
`browser = await browser_manager.ensure_browser()`:

```python
blocked = guard_url(request.url, browser_manager._settings, tracker)
if blocked is not None:
    return blocked
```

`browser_manager` already holds `_settings` (set in `__init__`); reading it is
fine (it's the same `Settings` stashed on `app.state.settings`). Alternatively
pass settings explicitly from the route — but `fetch_html` already takes
`browser_manager`, so `_settings` avoids threading a new param. (design decision:
`_settings` is a private attr but same-package access is consistent with the
codebase's pragmatic style; if a reviewer objects, promote to a property.)

`routes/fetch_image._do_fetch_image` — first lines inside `async with
browser_manager.concurrency:` (or just before it; guard first is better — no
point acquiring concurrency for a blocked URL). Use the same `guard_url` call;
on non-None, `return FetchImageResponseFail(reason="fetch_failed")`. Reuses the
existing `FailureTracker.record_failure` path already imported there.

## Why not a pydantic field_validator on `url`?

The existing `_url_must_be_absolute` validators (`schemas.py:47,91,134`) raise
`ValueError` → 422. Adding the private check there would also 422. Two reasons
not to:

1. **The escape hatch needs `Settings`.** A classmethod `field_validator` has
   no access to `app.state.settings`. Reading `os.environ` directly would
   violate the "Settings constructed once in lifespan" pattern (`config.py`
   docstring).
2. **Contract:** a 422 is a *validation* error surfaced to the caller with a
   detail message; the fetch contract is "never throw — `{ok:false,...}`"
   (`quality-guidelines.md` Forbidden Patterns). Keeping the guard on the
   fetch path (returning `HtmlFailed` → `fetch_failed`) honors the contract and
   records the block in the `FailureTracker` diagnostics trend, which a 422
   would skip.

Reusing `reason:"fetch_failed"` (not a new `"blocked_url"` reason) is the
zero-contract-change choice (R4). The block is still observable via logs +
`FailureTracker`; callers who want to distinguish can check logs. Tradeoff
noted: a caller cannot programmatically distinguish "network failed" from
"blocked by policy". Acceptable for v1 — surfacing a new reason is a contract
change and the parent's R1 forbids it.

## Out of scope (future work, documented not implemented)

- **DNS rebinding:** a hostname like `internal-service` resolves to a private
  IP only at fetch time. Resolving at fetch time (async `getaddrinfo` + IP
  check post-resolution) adds latency and a network call to the hot path and
  breaks the "no network in tests" invariant for the guard module. Defer; the
  literal-host guard closes the highest-severity hole (IP literals + known
  metadata hostnames) without that cost.
- **Per-caller URL allowlist:** out of scope; `fetch_allow_private` is the
  operator-level switch.

## Test plan (offline, pure + fake-app)

1. `tests/test_urlguard.py` — `is_private_target` table (positives + negatives
   above), `guard_url` returns `HtmlFailed` + records `blocked_url` when
   allow-private is False, returns None when True.
2. Extend `tests/test_extract_route.py` / a new route test: build a minimal
   FastAPI app with the `extract` router + a fake `BrowserManager` whose
   `ensure_browser` is a stub that must NOT be called when the URL is private —
   assert the exact `{ok:false,reason:"fetch_failed"}` body and that
   `ensure_browser` was never awaited. Assert a public URL still hits the stub.
3. `tests/test_fetch_route`-style equivalent for `/v1/fetch` and
   `/v1/fetch-image` private-target → `fetch_failed`.
