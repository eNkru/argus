# Implement — Inbound request rate limiting

> Ordered execution plan. `pytest` at every gate. No commit until review gate.

## Pre-flight

- Load `.trellis/spec/backend/quality-guidelines.md` + `error-handling.md`.
- `source .venv/bin/activate && pytest -q` → 37 green.

## Step 1 — pure limiter + unit tests (no middleware wiring)

1. Create `src/argus/ratelimit.py`: `_WindowCounter` + `RateLimitMiddleware`
   (full surface in `design.md`). No imports of `navigate`/`routes` →
   dependency-free, importable in tests without camoufox.
2. Add `Settings.rate_limit_enabled` / `rate_limit_requests` /
   `rate_limit_window_s` to `config.py` with the defaults in R2.
3. Add `tests/test_ratelimit.py`: `_WindowCounter` table (under/over/reset/
   key-isolation) using a tiny `window_s` for speed.
4. **Gate:** `pytest -q tests/test_ratelimit.py` green.

## Step 2 — middleware test against a minimal app (no main.py)

1. In `tests/test_ratelimit.py` (or `test_ratelimit_middleware.py`): build
   `FastAPI()` with `GET /x` + `GET /health`, wrap via
   `app.add_middleware(RateLimitMiddleware, settings=fake_settings)`. Use
   `Settings.model_construct(rate_limit_enabled=True, rate_limit_requests=2,
   rate_limit_window_s=60, ...)` per `conftest.make_settings`.
2. `TestClient`: 2× `/x` 200, 3rd `/x` 429 + `Retry-After`; `/health` always
   200; flip `rate_limit_enabled=False` → no 429 ever.
3. **Gate:** `pytest -q tests/test_ratelimit*.py` green.

## Step 3 — wire main.py

1. `main.py`: construct `Settings()` once at module scope (`_settings =
   Settings()`), pass to `RateLimitMiddleware` via `app.add_middleware(...)`
   right after `app = FastAPI(...)`. In `lifespan`, set
   `app.state.settings = _settings` (same instance — removes the prior
   in-lifespan `Settings()` construction). Keep all other lifespan setup
   (`BrowserManager`, `FailureTracker`, `build_ai_client`) unchanged.
2. Confirm `tests/conftest.py::make_settings` and the fake-app tests are
   unaffected (they construct their own settings; they don't import `main`).
3. **Gate:** `pytest -q` full suite green (existing 37 + new). This proves R1
   (default OFF → zero behavior change): the middleware is a pass-through when
   `rate_limit_enabled=False`.

## Step 4 — docs + env example

1. `.env.example`: add the three `ARGUS_RATE_LIMIT_*` keys with comments
   (default off; generous defaults; `/health` exempt; 429 + Retry-After).
2. `.trellis/spec/backend/`: add a short `security-guidelines.md` (or extend
   `quality-guidelines.md`) noting the rate-limit middleware, its single-process
   limitation, and the default-OFF contract.

## Validation (last iteration, full scope)

- `pytest -q` — all green.
- `grep -nIE "rate_limit|RateLimitMiddleware|429" src/ tests/` — wiring
  confirmed; no stray 429s introduced in fetch-path code (only the
  middleware emits 429).
- Confirm `pyproject.toml [project].dependencies` unchanged (no slowapi).

## Review gate

- R1 (byte-identical legit behavior) — default OFF; tests unchanged. ✓
- R2 (secure/opt-in default) — `rate_limit_enabled=False`. ✓
- R3 (no new runtime dep) — self-contained limiter. ✓
- R5 (pure-module tests, no camoufox) — `test_ratelimit*.py` offline. ✓

## Rollback

Revert `main.py` middleware line + the `config.py` fields. `ratelimit.py` +
tests can stay as dead code. The `_settings`-at-module-scope refactor reverts
to `Settings()` in lifespan (one-line).
