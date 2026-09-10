# Security Guidelines

> Hardening invariants for the Argus fetch service.

---

## SSRF — fetched-URL target blocklist

Every fetch route (`/v1/fetch`, `/v1/extract-price`, `/v1/fetch-image`)
navigates the browser to a caller-supplied `http(s)://` URL. Without a host
filter the service is a JS-executing SSRF proxy. **Fetched URLs are blocked
from private / loopback / link-local / cloud-metadata targets by default.**

- The classifier is `argus.urlguard.is_private_target` (pure, no network/DNS).
  It uses the `ipaddress` built-in properties (`is_loopback`, `is_link_local`,
  `is_private`, `is_unspecified`, `is_reserved`, `is_multicast`) for IP
  literals — including IPv4-mapped IPv6 — plus the well-known metadata
  hostnames (`localhost`, `metadata`, `metadata.google.internal`,
  `metadata.azure.com`).
- The guard runs in each route handler (`routes/fetch.py`,
  `routes/extract.py`, `routes/fetch_image.py`) **before** `begin_fetch` /
  concurrency / browser acquisition, via `urlguard.guard_url(url, settings)`.
- A blocked target maps to the route's existing `{ok:false, reason:"fetch_failed"}`
  contract — **no new reason enum** (zero response-contract change). The block
  is logged at `warning` (`ssrf block url=... host=...`); it is **not** recorded
  on the `FailureTracker` (a policy block is neither a fetch success nor a
  fetch failure; counting it would misattribute "browser degraded" after three
  SSRF probes — see `error-handling.md` pattern 4).
- `ARGUS_FETCH_ALLOW_PRIVATE=true` is the operator escape hatch for
  deployments that legitimately scrape internal hosts (bypasses the guard
  entirely). Default `false` (block) — the secure default.

**Forbidden:**
- Adding a new fetch entry point without calling `guard_url` at the top of the
  handler (before `begin_fetch`).
- Resolving hostnames to IPs at fetch time inside the guard (breaks the
  "no network in tests" invariant; DNS-rebinding hardening is deferred).

**Known limitation (deferred, not a bug):** the guard is literal-host only. A
hostname like `internal-svc` that resolves to a private IP is not blocked
without DNS resolution. The literal-host guard closes the highest-severity
hole (IP literals + known metadata hostnames); full DNS-rebinding protection
is future work.
