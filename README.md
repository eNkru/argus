# Argus

A general-purpose, Camoufox-backed anti-detect browser fetch service.

Argus holds one shared [Camoufox](https://camoufox.com/) browser (an engine-level
anti-detect Firefox fork) and exposes a small, bearer-token-authenticated HTTP API
for fetching page HTML and binary images through it. Each fetch runs in a fresh,
isolated `BrowserContext` — optionally with caller-supplied cookies / locale /
user-agent — so login-gated sites (e.g. Taobao) work by passing the logged-in
cookie jar, while login-free sites work with a plain `{url}`.

## Quickstart (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
camoufox fetch                         # download the Camoufox Firefox binary
export ARGUS_API_TOKENS=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn argus.main:app --reload --port 8000
```

## Quickstart (docker)

```bash
export ARGUS_API_TOKENS=change-me
docker compose up --build
```

## API

All `/v1/*` routes require `Authorization: Bearer <token>` where `<token>` is one
of the comma-separated values in `ARGUS_API_TOKENS`. `/health` is open.

| Method | Path              | Purpose                                            |
|--------|-------------------|----------------------------------------------------|
| GET    | `/health`         | Service readiness (browser lazy/absent is OK)      |
| POST   | `/v1/fetch`       | Fetch rendered page HTML (optional cookies/locale/UA)|
| POST   | `/v1/fetch-image` | Fetch a binary image through the browser           |

See [`docs/api-spec.md`](docs/api-spec.md) for the full OpenAPI contract and
[`docs/architecture.md`](docs/architecture.md) for the design rationale.

## Taobao (login-gated) flow

1. Log into Taobao in your own browser once.
2. Export the `.taobao.com` / `.tmall.com` cookie jar (devtools or a cookie
   export extension — the output already matches the `cookies` array shape).
3. Call `POST /v1/fetch` with `{url, cookies}` for every Taobao fetch.
4. For login-free sites, call `POST /v1/fetch` with `{url}` only.

## Layering note

Camoufox is the anti-detect Firefox engine; it is driven through Playwright's
Python API. There is **no Playwright Chromium** in this stack. `page.goto`,
`page.content()`, `context.cookies()`, `context.add_cookies()` are Playwright API
calls on the `Browser`/`BrowserContext` objects that `AsyncCamoufox` yields.
