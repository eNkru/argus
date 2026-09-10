# Inbound request rate limiting (generous, env-tuned, default off)

> Parent: `09-09-security-hardening`. Finding #4 (Low-Medium).

## Goal

Add inbound HTTP request rate limiting on `/v1/*` routes — including the
`401` auth-failure path, to slow bearer-token brute-force — with **generous
defaults that never trip for a legitimate single caller**, **default OFF** so
existing behavior is byte-identical until an operator opts in, and **no new
runtime dependency** (self-contained in-memory limiter, honoring the lean-deps
ethos in `ai.py`'s raw-httpx precedent).

Today there is no inbound throttling: `auth.py` validates the bearer token
with `secrets.compare_digest` (no timing oracle) but an attacker can hammer
`/v1/*` to brute-force the 256-bit token (infeasible to crack, but unthrottled
reconnaissance / abuse) or, once authenticated, drive the expensive
browser + LLM pipeline without bound. The only throttle today is on the
*outbound* LLM calls (`ai.py`), not inbound requests.

## Requirements

- **R1** New pure module `src/argus/ratelimit.py` with a fixed-window
  in-memory limiter keyed by client IP (`request.client.host`), single-process
  (argus runs one uvicorn process; no shared store needed).
- **R2** `Settings` fields (all `ARGUS_RATE_LIMIT_*`):
  - `rate_limit_enabled: bool = False` (default OFF → zero behavior change)
  - `rate_limit_requests: int = 600` (requests per window per IP; generous — a
    single caller at 1 req/s is 60/min, well under 600)
  - `rate_limit_window_s: int = 60` (window seconds)
- **R3** Middleware added in `main.py` lifespanpan/constructor that, when
  `rate_limit_enabled`, applies the limiter to all routes **except `/health`**
  (healthcheck probes must not be throttled — it polls every 15s per
  `docker-compose.yml`). Over-limit → `429 Too Many Requests` with a
  `Retry-After` header and an `INFO` log (`ip=%s requests=%d window=%d`).
  When disabled, the middleware is a pass-through (no allocations).
- **R4** The `401` auth path is rate-limited like any `/v1/*` request (it runs
  before the handler). This is the brute-force slow-down. Legitimate callers
  with a valid token are unaffected (generous default).
- **R5** No new runtime dependency. No `slowapi`. Plain `asyncio` + a `dict`
  + monotonic time (mirrors `ai.py`'s throttle style).
- **R6** Tests: pure unit tests for the limiter window/eviction logic
  (offline, no FastAPI); a middleware test with a minimal FastAPI app + the
  fake-`app.state` pattern asserting 429 after N+1 and 200 under the limit;
  assert `rate_limit_enabled=False` → all requests pass (zero behavior change).

## Acceptance criteria

- [ ] `src/argus/ratelimit.py` with the limiter + middleware factory as
  specified (R1, R3, R5).
- [ ] `Settings.rate_limit_enabled` defaults `False`; `rate_limit_requests=600`,
  `rate_limit_window_s=60` (R2); documented in `.env.example`.
- [ ] `/health` exempt; over-limit → `429` + `Retry-After` + INFO log (R3).
- [ ] `401` path covered (R4).
- [ ] Default OFF → existing 37-test suite green with zero behavior change;
  new offline tests added (R6).
- [ ] No new runtime dep (R5); `pyproject.toml` `[project].dependencies`
  unchanged.

## Constraints

- Inherits parent R1–R6. The **R1 (byte-identical legit behavior)** constraint
  is satisfied by defaulting OFF; when ON with generous defaults, a single
  legit caller never sees a 429.
- R3 (no new runtime dep) — self-contained limiter.
- R5 (no browser/network in tests) — limiter is pure; middleware test uses a
  trivial in-process FastAPI app, no camoufox lifespan.

## Coordination

- `main.py` is also touched by `prod-docs-toggle` (FastAPI constructor kwargs).
  Apply docs-toggle's `docs_url=...` and this child's `app.add_middleware(...)`
  in the same pass if both land together; they edit different regions
  (constructor vs middleware), no conflict.

## Notes

- Single-process assumption is documented in the module docstring. If argus
  ever moves behind multiple workers / a load balancer, the in-memory limiter
  becomes per-worker — note as a known limitation, do not fix here (would
  require Redis/external store = a new dep, violating R3).
