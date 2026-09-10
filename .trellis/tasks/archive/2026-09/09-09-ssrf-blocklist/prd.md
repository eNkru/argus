# SSRF fetch-target blocklist (link-local / metadata / RFC1918), env-gated

> Parent: `09-09-security-hardening`. Finding #1 (Medium-High).

## Goal

Stop the three fetch endpoints (`/v1/fetch`, `/v1/fetch-image`,
`/v1/extract-price`) from navigating the browser to private / loopback /
link-local / cloud-metadata targets, **by default**, while leaving every
legitimate public-URL fetch byte-identical and providing an explicit operator
escape hatch (`ARGUS_FETCH_ALLOW_PRIVATE=true`) for deployments that
legitimately scrape internal hosts.

Today the `url` field validators (`schemas.py:47,91,134`) only assert the
scheme is `http(s)://`. There is no host filtering, so an authenticated caller
(or any caller when `ARGUS_AUTH_DISABLED=true`) can drive the browser at
`http://169.254.169.254/...` (cloud IAM metadata), `127.0.0.1`,
`10.x`/`192.168.x`/`172.16-31.x`, or `localhost`, and the response HTML / base64
body is returned to them — a JS-executing SSRF proxy, worse than plain HTTP.

## Requirements

- **R1** New pure module `src/argus/urlguard.py` exposing
  `is_private_target(url: str) -> bool`. Pure, no network, no DNS — operates on
  the URL's literal host. Returns `True` when the host is:
  - an IPv4 literal in `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`,
    `192.168.0.0/16`, or `169.254.0.0/16` (link-local, incl. `169.254.169.254`);
  - an IPv6 literal that is loopback (`::1`) or link-local (`fe80::/10`), or an
    IPv4-mapped private form of the above;
  - the well-known hostnames `localhost`, `metadata.google.internal`,
    `metadata`, `metadata.azure.com` (case-insensitive, no trailing dot).
- **R2** `Settings.fetch_allow_private: bool = False` in `config.py`
  (`ARGUS_FETCH_ALLOW_PRIVATE`). Default `false` = block. `true` = escape
  hatch (guard skipped entirely).
- **R3** The guard runs at the **top** of `navigate.fetch_html` (covers
  `/v1/fetch` + `/v1/extract-price`) **and** at the top of
  `routes/fetch_image._do_fetch_image` (covers `/v1/fetch-image`, which does not
  use `navigate.fetch_html`), **before** any `browser_manager` / concurrency
  acquisition. A shared helper `urlguard.guard_url(url, settings, tracker)
  -> HtmlFailed | None` does the settings-check + logging + `record_failure` so
  the logic is not duplicated.
- **R4** Blocked targets map to the existing `{ok:false, reason:"fetch_failed"}`
  contract for fetch + extract, and `{ok:false, reason:"fetch_failed"}` for
  fetch-image — **no new `reason` enum value** (zero contract change). The block
  is recorded on the `FailureTracker` with `kind="blocked_url"` and logged at
  `INFO` with the URL only (no secrets).
- **R5** With `ARGUS_FETCH_ALLOW_PRIVATE=true`, the guard is a no-op —
  operator escape hatch, exact prior behavior.
- **R6** Unit tests for `urlguard.is_private_target` (pure): positive
  (169.254.169.254, 127.0.0.1, 10.1.2.3, 172.20.0.5, 192.168.0.1, ::1, fe80::1,
  localhost, metadata.google.internal) and negative (public IPs, public
  hostnames). Route-level test via the fake-`app.state` pattern asserting the
  exact `{ok:false,reason:"fetch_failed"}` body on a private target and
  unchanged body on a public target.

## Acceptance criteria

- [ ] `src/argus/urlguard.py` exists with `is_private_target` + `guard_url` as
  specified (R1, R3).
- [ ] `Settings.fetch_allow_private` defaults `False` (R2); documented in
  `.env.example`.
- [ ] Guard applied in both `navigate.fetch_html` and
  `routes/fetch_image._do_fetch_image` before browser/concurrency (R3).
- [ ] Blocked target → `{ok:false,reason:"fetch_failed"}` in all three routes,
  `kind="blocked_url"` recorded, URL logged at INFO, no secret in log (R4).
- [ ] `ARGUS_FETCH_ALLOW_PRIVATE=true` bypasses the guard (R5).
- [ ] Existing 37-test suite green; new offline tests added (R6).
- [ ] Legitimate public-URL fetch/extract/fetch-image bodies unchanged
  (existing tests unchanged).

## Constraints

- Inherits parent R1–R6. Notably R3 (no new runtime dep) and R5 (pure module,
  no browser/network in tests).
- **Out of scope (note in design, do not implement):** DNS-rebinding
  resolution (resolving a hostname to an IP at fetch time) is a deeper
  hardening that risks latency and testability regressions; the v1 guard is
  literal-host only. Document as future work.

## Coordination

- None — `urlguard.py` is a new module; `navigate.py` and
  `routes/fetch_image.py` edits are additive at the top of two functions and
  do not overlap other children's files.
