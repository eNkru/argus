# Design — Inbound request rate limiting

## Why no `slowapi`

`slowapi` is the usual FastAPI rate-limit lib, but: (a) it's a new runtime dep
violating parent R3; (b) the codebase has an explicit lean-deps ethos
(`ai.py` module docstring: "lean-deps choice — no `openai` SDK, full
retry/throttle control"); (c) the requirement is a single-process fixed-window
limiter, which is ~40 lines. Mirrors `ai.py`'s module-level throttle pattern
(`_throttle_semaphore`, `_last_call_ended_at`, `time.monotonic()`).

## Module surface

```python
# src/argus/ratelimit.py
from __future__ import annotations
import asyncio
import logging
import time
from starlette.types import ASGIApp, Receive, Scope, Send
from .config import Settings

logger = logging.getLogger("argus.ratelimit")

class _WindowCounter:
    """Fixed-window counter per key (client IP). Not thread-safe across
    processes — single-process uvicorn only (documented limitation)."""
    def __init__(self, window_s: int) -> None: ...
    def check(self, key: str, limit: int) -> tuple[bool, int]:
        """(allowed, current_count_after_increment). Resets when window rolls."""

class RateLimitMiddleware:
    """ASGI middleware. No-op when Settings.rate_limit_enabled is False."""
    def __init__(self, app: ASGIApp, settings: Settings) -> None: ...
    async def __call__(self, scope, receive, send) -> None: ...
```

## Algorithm — fixed window

- Per client IP (`scope["client"][0]`), maintain a `(window_start, count)`.
- On request: if `now - window_start >= window_s`, reset
  `window_start = now`, `count = 0`. Increment `count`. If `count > limit`,
  reject with 429.
- Fixed window (not sliding) is fine for this threat model (brute-force /
  abuse dampening, not billing-accurate fairness). Simpler, fewer allocations.

## Exemptions + response shape

- `scope["path"] == "/health"` → always pass (the compose healthcheck polls
  every 15s; even the generous 600/60 would never trip, but exempting removes
  any doubt and matches the unauthenticated-readiness-probe contract in
  `routes/health.py`).
- 429 response: a minimal JSON body `{"detail":"rate limit exceeded"}` (NOT a
  `{ok:false,...}` body — this is a transport-level rejection outside the fetch
  contract, consistent with how FastAPI renders `429`/`422`/`401`). Include
  `Retry-After: <remaining window seconds>` header. Log at INFO
  (`ip=%s requests=%d window=%d path=%s`).
- The `401` path: the middleware wraps the whole app, so an unauthenticated
  `/v1/*` call hits the limiter before `auth.require_token` runs — exactly the
  brute-force slow-down (R4). A legit caller with a wrong token retrying a few
  times is far under 600/60.

## Wiring in main.py

`main.py` currently: `app = FastAPI(title="Argus", lifespan=lifespan)`. Add,
after `app` is constructed (lifespan still sets `app.state.settings`):

```python
app = FastAPI(title="Argus", lifespan=lifespan, **_docs_kwargs(settings))
# settings is read in lifespan, but the limiter middleware can be added
# after lifespan via app.add_middleware — middleware is composed at startup.
```

Problem: `Settings()` is constructed inside `lifespan`, not at module load, so
`main.py` at import time does not yet have settings. Two options:

1. Construct `Settings()` at module top of `main.py` for the middleware factory
   only, AND again in lifespan for `app.state`. Risk: two constructions; if
   env changes between, drift. `Settings` is cheap and env is stable at boot,
   but it's a smell.
2. Add the middleware inside `lifespan` after constructing settings:
   `app.add_middleware(...)` before `yield`. FastAPI/Starlette allows
   `add_middleware` in lifespan? Actually middleware must be added before the
   app starts serving; `add_middleware` after startup is not applied. The
   correct ASGI pattern: wrap the app at construction.

**Decision:** construct `Settings()` once at module load in `main.py` for
middleware construction, and reuse the same instance in `lifespan`
(`app.state.settings = _settings`). This removes the double-construction smell
and gives the middleware the settings it needs at app construction time. This
is a small, safe refactor: `Settings` reads env once; the instance is the
source of truth for both the middleware and `app.state`. Verify
`tests/conftest.py::make_settings` still works (it constructs its own
`Settings.model_construct(...)` for fake apps — unaffected; production
`main.py` just builds one real instance).

If a reviewer prefers not to touch lifespan shape, fallback: build settings
in `main.py` module scope, pass to middleware, and in lifespan do
`app.state.settings = _MODULE_SETTINGS` (same instance). Same outcome.

## Test plan (offline)

1. `tests/test_ratelimit.py` — `_WindowCounter`: under-limit pass, over-limit
   block, window reset after `window_s` (use a monkeypatched clock or a
   0.05s window for speed), key isolation.
2. Middleware: build a trivial `FastAPI()` with a single `GET /x` returning
   `{"ok":True}` + `GET /health`, wrap with `RateLimitMiddleware(settings)`
   where settings has `rate_limit_enabled=True, rate_limit_requests=2,
   rate_limit_window_s=60`. Assert 3rd `/x` → 429 + `Retry-After`, `/health`
   always 200, and with `rate_limit_enabled=False` all pass (no 429).
3. Confirm the real `main.py` app is NOT instantiated in tests (no camoufox)
   — these tests use the small purpose-built app per `quality-guidelines.md`.

## Out of scope

- Distributed / multi-worker limiting (needs external store = new dep).
- Per-token (vs per-IP) limiting — per-IP is the v1 choice; per-token would
  require auth to run first, complicating the middleware ordering. Defer.
