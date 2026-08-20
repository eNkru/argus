# Directory Structure

> How backend code is organized in this project.

---

## Overview

Argus is a single Python package (`src/argus/`), Python ≥3.11, FastAPI + Pydantic v2, driven by Playwright's async API over the Camoufox anti-detect Firefox engine. There is no database, no ORM, and no worker queue — it is a stateless fetch transport service.

---

## Directory Layout

```
src/argus/
├── main.py           # FastAPI app + lifespan (singletons on app.state)
├── config.py         # pydantic-settings (all ARGUS_* env vars)
├── auth.py           # bearer-token dependency (constant-time compare)
├── browser.py        # shared Camoufox: lazy launch, idle teardown, single-flight
├── render.py         # SPA render-wait + resilient content snapshot
├── signatures.py     # anti-bot blocked-page registry (pluggable)
├── diagnostics.py    # consecutive-failure tracking + degraded-browser logs
├── schemas.py        # pydantic request/response models (incl. Cookie shape)
├── cookies.py        # cookie normalization to the add_cookies shape
└── routes/
    ├── health.py     # GET /health        (unauthenticated)
    ├── fetch.py      # POST /v1/fetch     (bearer-gated)
    └── fetch_image.py# POST /v1/fetch-image (bearer-gated)

tests/                # pytest, asyncio_mode=auto; pure modules only, no browser needed
docs/                 # api-spec.md, architecture.md, future.md
```

---

## Module Organization

- **Flat single package** — no nested service/repository layers. Each module owns one concern and carries a module-level docstring explaining *why* it exists (see `src/argus/browser.py`, `src/argus/signatures.py` for reference-quality examples).
- **Routes live in `routes/`** — one file per endpoint group, each defining its own `APIRouter`. Auth is attached via router-level dependencies (`router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])` in `routes/fetch.py`), never per-handler.
- **Singletons on `app.state`, never module globals** — `main.lifespan` constructs `Settings`, `BrowserManager`, `FailureTracker` once and stashes them; route handlers read `req.app.state.*`. Do not reintroduce module-global lifecycle state (this was deliberately refactored away from the iris sidecar pattern).
- **Business logic lives in plain modules** (`browser.py`, `render.py`, `signatures.py`), routes stay thin: validate → delegate → map to response model.
- **New env var?** Add to `Settings` in `config.py` (prefix `ARGUS_`, annotated with a comment) and to `.env.example` + README config table.

---

## Naming Conventions

- Files: `snake_case.py`, one concern per file.
- Loggers: `logging.getLogger("argus.<module>")` — e.g. `argus.fetch`, `argus.browser`, `argus.render`, `argus.diagnostics`.
- Public API JSON fields use camelCase (`waitUntil`, `renderWaitMs`, `timeoutMs`, `contentType`) to mirror browser devtools shapes; Python identifiers stay snake_case.
- Playwright/browser-facing shapes (the `Cookie` model) intentionally match the devtools "copy all cookies" export verbatim so jars can be pasted unmodified.

---

## Examples

- Well-organized module with lifecycle comments: `src/argus/browser.py`
- Thin route handler delegating to modules: `src/argus/routes/fetch.py`
- Pluggable registry pattern: `src/argus/signatures.py`
