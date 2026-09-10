# Disable /docs /redoc /openapi.json in production (env-gated)

> Parent: `09-09-security-hardening`. Finding #3 (Low-Medium). Lightweight —
> `main.py` + `config.py` + `dev.sh`.

## Goal

Stop exposing the interactive API docs (`/docs`, `/redoc`) and the full OpenAPI
schema (`/openapi.json`) unauthenticated in production, **defaulting to OFF in
production and ON in local dev** (so `dev.sh` keeps `/docs`).

Today `main.py:49` is `app = FastAPI(title="Argus", lifespan=lifespan)` — no
`docs_url`/`redoc_url`/`openapi_url` override, so FastAPI's defaults serve all
three publicly. The schema documents every endpoint, request/response shape, and
the bearer auth scheme — free reconnaissance. The `/v1/*` operations themselves
still require a token; this is about not handing attackers the map.

## Requirements

- **R1** `Settings.docs_enabled: bool = False` (`ARGUS_DOCS_ENABLED`). Default
  `False` = docs off (production-safe). `True` = serve `/docs`, `/redoc`,
  `/openapi.json`.
- **R2** `main.py`: pass `docs_url`/`redoc_url`/`openapi_url` to `FastAPI(...)`,
  each `None` when `not docs_enabled` (disabled) else the default (omit / pass
  the default URL). `/health` is unaffected (it's a real route, not a docs
  route).
- **R3** `dev.sh`: export `ARGUS_DOCS_ENABLED=true` before launching uvicorn so
  local dev keeps `/docs` (the README's "Docs: http://localhost:PORT/docs"
  banner stays accurate).
- **R4** `.env.example`: document `ARGUS_DOCS_ENABLED=false` (prod default) with
  a comment that dev.sh flips it on.

## Acceptance criteria

- [ ] `Settings.docs_enabled` defaults `False`.
- [ ] With `ARGUS_DOCS_ENABLED` unset → `GET /docs` 404, `/redoc` 404,
  `/openapi.json` 404; `/health` 200.
- [ ] With `ARGUS_DOCS_ENABLED=true` → `/docs` 200, `/openapi.json` 200.
- [ ] `dev.sh` sets `ARGUS_DOCS_ENABLED=true` (docs available locally).
- [ ] Existing 37-test suite green (these tests use purpose-built small apps,
  not `main.app` — so unaffected); add a small test asserting the toggle using a
  minimal `FastAPI(docs_url=...)` app.
- [ ] No Python fetch/auth behavior change (R1 of parent).

## Implementation notes

- Constructing `Settings()` at module scope in `main.py` (also done by
  `inbound-rate-limiting`) gives `docs_enabled` for the `FastAPI(...)` kwargs.
  Coordinate so both children use the same module-scope `_settings` instance.
- Do NOT gate `/health` — it's intentionally unauthenticated
  (`routes/health.py`).

## Coordination

- `main.py` is also touched by `inbound-rate-limiting`. Both introduce a
  module-scope `Settings()` + reuse it in `lifespan`. Land them in one pass so
  the `_settings` instance is shared (don't construct it twice).
