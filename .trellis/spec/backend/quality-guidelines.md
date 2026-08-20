# Quality Guidelines

> Code standards, testing requirements, forbidden patterns.

---

## Verification Commands

```bash
source .venv/bin/activate
pytest          # full suite (37 tests) — must pass; no browser binary or network needed
```

There is no linter/formatter configured. If one is added, wire it here and into CI before enforcing it.

---

## Code Standards

- **Python ≥ 3.11**; use `from __future__ import annotations` at the top of every module (universal in this codebase).
- **Pydantic v2 idioms**: `BaseSettings` + `SettingsConfigDict` for config; `field_validator` (with `@classmethod`) for validation; `model_dump()` for serialization. No pydantic v1 patterns (`validator=`, `.dict()`).
- **Type hints everywhere**, including test functions (`-> None`). `Optional[X]` / `X | None` both appear; prefer `X | None` in new code.
- **Module docstrings explain *why***, not just what — reference real incidents/dates where the behavior was proven live (see `signatures.py` Cloudflare notes: "confirmed pbtech PDP 2026-08-04").
- **Deliberate broad catches** carry `# noqa: BLE001 — <reason>` (see error-handling.md).
- **Playwright async API only** — no sync Playwright, no Playwright Chromium; Camoufox is the only engine (`AsyncCamoufox` from `camoufox.async_api`).
- **Dependency pins**: `camoufox==0.5.4` is pinned to the browser build that passed the anti-bot spike; bump deliberately and re-run the pass-rate matrix (comment in `pyproject.toml`).

---

## Testing Requirements

- **pytest + pytest-asyncio**, `asyncio_mode = "auto"`, `testpaths = ["tests"]` (`pyproject.toml`).
- **Tests cover pure modules only** — no Camoufox binary, no network. Browser-dependent paths are exercised by manual curl smoke tests (see README "Test locally").
- **Never spin up the real lifespan in tests** (it imports camoufox). Instead build minimal apps and fake `app.state`:
  - `Settings.model_construct(...)` to bypass env reading (`tests/conftest.py::make_settings`).
  - `SimpleNamespace` for fake managers (`tests/test_health.py::_make_app`).
  - Small purpose-built FastAPI apps with just the router under test + `TestClient`.
- **Assertion style**: plain `assert response.json() == {...}` against the full expected JSON body — tests assert exact response contracts.
- New pure-module code must ship with tests; new routes need at minimum schema/auth-level tests runnable offline.

---

## Forbidden Patterns

| Pattern | Why |
|---|---|
| Module-global mutable lifecycle state | Deliberately removed from the iris sidecar port; singletons live on `app.state` via lifespan |
| Logging cookies/tokens/jar contents | Secrets — log counts only |
| Raising to the caller on the fetch path | Contract: always `{ok:false, ...}` JSON (auth 401 is the only exception) |
| Sync Playwright / Chromium engine | Stack is Camoufox-only by design |
| Per-retailer branching in the fetch path | Blocked-page handling is a generic signature registry (`signatures.py`); add signatures, not site-specific code |
| f-strings in log calls | Lazy `%s` formatting only |
| Skipping `finally` cleanup of contexts/pages | Cookie-jar isolation between callers is a security invariant |

---

## Common Mistakes

- Reusing a `BrowserContext` across requests — every fetch gets a fresh context, closed in `finally`.
- Treating `challenges.cloudflare.com` in HTML as a guaranteed block — real PDPs embed Turnstile; size-capped shell detection only (see `signatures.py`).
- Forgetting `browser_manager.begin_fetch()` / `end_fetch()` pairing (finally!) around new fetch entry points — it gates idle teardown.
