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

---

## Container — non-root runtime user, no network utilities

The image runs the uvicorn + Camoufox process tree as a **non-root user**
(`argus`, uid/gid 1000). A browser or Playwright compromise therefore lands as
an unprivileged user, not root (2026-09 hardening, finding #2).

- `Dockerfile` creates `argus` with `--create-home --home-dir /home/argus` and
  sets `ENV HOME=/home/argus` **before** the `camoufox fetch` layer, so the
  browser cache lands in `/home/argus/.cache/camoufox/browsers/official/*/` —
  owned by the runtime user instead of stranded in `/root`.
- All build `RUN`s stay root (venv creation, lockfile install, browser fetch,
  `pip install --no-deps .`); `chown -R argus:argus /opt/argus /home/argus`
  runs once after installs; `USER argus` sits directly before `CMD`.
- The macos/windows font prune paths MUST match `HOME` — they live under
  `/home/argus/.cache/...`. If `HOME` ever moves again, move the prune paths
  with it (keep the prune: it trims ~900MB of dead-weight fonts).
- **Healthcheck is Python, not wget**: the compose probe is
  `python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health', timeout=4); sys.exit(0)"`
  — the venv interpreter (PATH already includes `/opt/argus/bin`). `wget` was
  removed from the apt list; **do not re-add it** and do not introduce other
  network utilities (curl, netcat) into the image — the browser is the only
  outbound client that matters and it ships its own network stack.

**Forbidden:**
- Running the container as root (adding work after `USER argus`, or reordering
  so `CMD` precedes the user switch).
- Editing the Dockerfile without re-verifying `docker compose up` reaches
  `healthy` AND `docker compose exec argus id` shows `uid=1000(argus)` — a
  root-running or broken-permission image otherwise ships silently.
- Removing GTK/NSS/X11 apt libs (Camoufox needs them) or `build-essential`
  (needed for wheel compilation at pip-install stage).

---

## Inbound request rate limiting — per-IP fixed window

`ratelimit.RateLimitMiddleware` wraps the whole app via `app.add_middleware`
(in `main.py`), so it runs **before** `auth.require_token` — the 401
bearer-token brute-force path is throttled exactly like any other `/v1/*`
request, and once authenticated, an abusive caller is bounded too.

- **Default OFF.** `Settings.rate_limit_enabled` defaults `False`, so behavior
  is byte-identical until an operator sets `ARGUS_RATE_LIMIT_ENABLED=true`
  (secure, opt-in default). When disabled the middleware is a pass-through
  with no per-request work.
- **Generous defaults.** `rate_limit_requests=600` per `rate_limit_window_s=60`
  seconds per client IP — a single legit caller at 1 req/s (60/min) never trips
  it; it exists to dampen brute-force / recon abuse, not to meter fair use.
- **`/health` is exempt** — the compose readiness probe polls every 15s and must
  never be throttled.
- **Over-limit → 429** with `Retry-After` (remaining window seconds) and an INFO
  log (`ip=%s requests=%d window=%d path=%s`), a transport-level rejection — not
  a `{ok:false,...}` fetch-contract body (consistent with FastAPI's own 401/422).
- **Single-process, in-memory, no new dependency.** State is one `dict` keyed by
  `request.client.host` with `time.monotonic()` fixed windows, mirroring
  `ai.py`'s lean-deps throttle. No `slowapi`, no Redis/external store.

**Known limitation (deferred, not a bug):** the limiter is per-process. If argus
moves behind multiple uvicorn workers or a load balancer, each worker gets its
own budget (a shared store would be a new runtime dep). Fixed-window (not
sliding) is also deliberate — sufficient for dampening, not billing-accurate
fairness.

**Forbidden:**
- Adding a new `/v1/*` entry point and expecting it to be throttled for free —
  the middleware covers the whole app, but if you ever add a router _outside_
  the middleware's scope or re-mount, re-check the throttle applies before auth.
- Replacing the in-memory limiter with a client lib (`slowapi`) or external
  store without updating this guideline and the no-new-runtime-dep constraint.

