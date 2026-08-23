# Argus

A general-purpose, Camoufox-backed anti-detect browser fetch service.

Argus holds one shared [Camoufox](https://camoufox.com/) browser (an engine-level
anti-detect Firefox fork) and exposes a small, bearer-token-authenticated HTTP API
for fetching page HTML and binary images through it. Each fetch runs in a fresh,
isolated `BrowserContext` — optionally with caller-supplied cookies / locale /
user-agent — so login-gated sites (e.g. Taobao) work by passing the logged-in
cookie jar, while login-free sites work with a plain `{url}`.

Useful when a target site sits behind an anti-bot WAF (Cloudflare, Akamai,
DataDome) that 403s plain HTTP clients: argus passes the challenge because the
fetch carries a full anti-detect browser fingerprint.

## Layering note

Camoufox is the anti-detect Firefox engine; it is driven through Playwright's
Python API. There is **no Playwright Chromium** in this stack. `page.goto`,
`page.content()`, `context.add_cookies()` are Playwright API calls on the
`Browser`/`BrowserContext` objects that `AsyncCamoufox` yields.

## API

All `/v1/*` routes require `Authorization: Bearer <token>` where `<token>` is one
of the comma-separated values in `ARGUS_API_TOKENS`. `/health` is open.

| Method | Path              | Purpose                                               |
|--------|-------------------|-------------------------------------------------------|
| GET    | `/health`         | Service readiness (browser lazy/absent is OK)         |
| POST   | `/v1/fetch`       | Fetch rendered page HTML (optional cookies/locale/UA)  |
| POST   | `/v1/extract-price` | Fetch + parse product price — JSON-LD first, AI fallback |
| POST   | `/v1/fetch-image` | Fetch a binary image through the browser              |

The service never throws to the caller: navigation/timeout errors map to
`{ok:false, reason:"fetch_failed"}`. When a WAF serves a challenge/deny page,
argus classifies it via its signature registry and returns
`{ok:false, reason:"blocked", signature, retryable}`.

Full OpenAPI contract: [`docs/api-spec.md`](docs/api-spec.md). Design rationale:
[`docs/architecture.md`](docs/architecture.md). Interactive docs are served by
the app itself at `http://localhost:8000/docs` (Swagger UI).

## Quick reference

```bash
# fetch a page
curl -s -X POST http://localhost:8000/v1/fetch \
  -H "content-type: application/json" \
  -H "authorization: Bearer $ARGUS_API_TOKEN" \
  -d '{"url":"https://example.com"}'
# -> {"ok":true,"html":"<!DOCTYPE html>...","url":"https://example.com/"}

# fetch an image (base64 in JSON)
curl -s -X POST http://localhost:8000/v1/fetch-image \
  -H "content-type: application/json" \
  -H "authorization: Bearer $ARGUS_API_TOKEN" \
  -d '{"url":"https://example.com/logo.png"}'
# -> {"ok":true,"contentType":"image/png","data":"iVBORw0..."}

# login-gated site: pass the cookie jar (shape matches a browser
# devtools "copy all cookies" export verbatim)
curl -s -X POST http://localhost:8000/v1/fetch \
  -H "content-type: application/json" \
  -H "authorization: Bearer $ARGUS_API_TOKEN" \
  -d '{
    "url": "https://www.taobao.com/...",
    "cookies": [
      {"name":"login5","value":"...","domain":".taobao.com","path":"/","httpOnly":true,"secure":true}
    ]
  }'
```

Optional per-request knobs on `/v1/fetch` (all have defaults):
`waitUntil` (`domcontentloaded` | `load` | `networkidle`), `renderWaitMs` (8000),
`timeoutMs` (35000), `detectBlocked` (true), `locale`, `userAgent`. See
`docs/api-spec.md` — and the `userAgent` fingerprint caveat in
`docs/architecture.md` before overriding it.

---

## Run locally

Prerequisites: Python 3.11+.

### One command

```bash
./dev.sh
```

`dev.sh` is idempotent — restarts are instant. On first run it:

1. creates the virtualenv (`.venv`) and installs editable deps with dev extras
2. fetches the Camoufox browser binary (~200-900 MB, cached in
   `~/Library/Caches/camoufox` on macOS; only re-done on version bumps)
3. generates a dev bearer token into `.env` (gitignored), so the server shell
   and any curl shell share the same token across runs
4. starts `uvicorn argus.main:app --reload --port 8000`

On subsequent runs it skips each step that's already satisfied and just starts
the server. The bearer token is printed in the startup banner; rotate it by
deleting the `ARGUS_API_TOKENS=` line in `.env` and re-running. An explicit
`ARGUS_API_TOKENS` env var takes precedence over `.env`.

The server is up when `curl http://localhost:8000/health` returns
`{"status":"ok","browser":"absent"}`. `browser: "absent"` is expected at boot —
the browser launches lazily on the first `/v1/fetch` and tears down after 5 min
idle (configurable via `ARGUS_IDLE_TIMEOUT_SECONDS`), so an idle dev machine
doesn't hold ~500 MB of Firefox resident.

### Interactive API docs (Swagger UI)

With the server running, open:

- `http://localhost:8000/docs` — Swagger UI; click **Authorize** 🔒, paste the
  bearer token, then "Try it out" on any `/v1/*` endpoint — the header is sent
  automatically, no hand-crafted curl.
- `http://localhost:8000/redoc` — read-only schema browser.
- `http://localhost:8000/openapi.json` — raw OpenAPI 3 contract.

### Manual setup (what `dev.sh` automates)

```bash
cd argus

# 1. venv + dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. one-time: download the Camoufox Firefox binary
camoufox fetch

# 3. generate a bearer token and export it
export ARGUS_API_TOKENS=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# 4. start the server
uvicorn argus.main:app --reload --port 8000
```

### Smoke test with curl

```bash
# token from .env (written by dev.sh) or the one you exported above
export ARGUS_API_TOKEN=<your token>

curl -s -X POST http://localhost:8000/v1/fetch \
  -H "content-type: application/json" \
  -H "authorization: Bearer $ARGUS_API_TOKEN" \
  -d '{"url":"https://example.com"}' | python3 -m json.tool | head

curl -s http://localhost:8000/health
# after the first fetch: {"status":"ok","browser":"ready"}
```

The token must be the same in the server shell and any shell you curl from —
`dev.sh` handles this by persisting it to `.env`, which argus reads
automatically (see `.env.example` for all `ARGUS_*` vars).

## Test locally

```bash
source .venv/bin/activate
pytest                      # 37 unit tests: auth, schemas, cookies, signatures, health
```

The unit tests cover the pure modules — they do **not** need the camoufox
browser binary or network access. The browser-dependent paths (end-to-end
`/v1/fetch`, lazy launch, idle teardown) are exercised by running the server
locally and curling real URLs as above; treat a manual curl against a
Cloudflare-protected URL as the "blocked classification" smoke test:

```bash
curl -s -X POST http://localhost:8000/v1/fetch \
  -H "content-type: application/json" \
  -H "authorization: Bearer $ARGUS_API_TOKEN" \
  -d '{"url":"https://<some-cloudflare-protected-site>"}'
# expect on a challenge page:
# {"ok":false,"reason":"blocked","signature":"cloudflare-challenge","retryable":true}
```

## Deploy with Docker

### docker compose (recommended)

Configuration is `.env`-driven: the compose file bind-mounts a `.env` from this
directory into the container at `/app/.env` (read-only) — the path where
argus reads it at startup. Secrets never appear in `docker inspect`.

```bash
cd argus

# 1. copy the template and set a strong bearer token (comma-separated for multiple/rotation)
cp .env.example .env    # do this BEFORE the first up — see note below
#    generate one: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
#    then edit .env: ARGUS_API_TOKENS=<your token>  (and ARGUS_AI_* if using the AI fallback)

# 2. build & start (reads .env automatically)
docker compose up --build -d

# readiness
curl http://localhost:8000/health
```

> **Create `.env` before the first `up`.** A missing file makes Docker create an
> empty *directory* at the mount path, which crashes the app with a confusing
> error instead of a clear "file not found".

Config edits apply on **restart**, not live — argus reads `.env` once at boot:
`docker compose restart argus`.

If `ARGUS_API_TOKENS` is left empty, the container still boots but every `/v1/*`
call rejects with 403 (fail-closed) — set it before going live.

The image is self-contained: it installs the camoufox venv, downloads the
browser binary at **build** time (so production startup needs no internet), and
prunes the unused macos/windows font sets (~890 MB saved; the image pins the
linux fingerprint whose fonts are the only ones physically present).

Point callers at `http://<host>:8000` with the same bearer token.

### Plain docker

```bash
docker build -t argus .
docker run -d --name argus \
  -p 8000:8000 \
  -e ARGUS_API_TOKENS=your-token \
  argus
```

### QNAP NAS

For running on a QNAP NAS via Container Station (architecture detection, build
strategy, reverse proxy for HTTPS, autostart, and NAS-specific troubleshooting):
see [`docs/deploy-qnap.md`](docs/deploy-qnap.md).

### Configuration (env vars)

All prefixed `ARGUS_`; see `.env.example` for the full annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `ARGUS_API_TOKENS` | *(empty)* | Comma-separated valid bearer tokens (rotation / per-client). **Required** unless auth disabled. |
| `ARGUS_AUTH_DISABLED` | `false` | Bypass bearer auth — local dev only. |
| `ARGUS_CONCURRENCY` | `5` | Max concurrent fetches (asyncio semaphore). |
| `ARGUS_IDLE_TIMEOUT_SECONDS` | `300` | Tear the browser down after this much idle. |
| `ARGUS_FETCH_TIMEOUT_SECONDS` | `35` | Per-request navigation timeout. |
| `ARGUS_RENDER_WAIT_SECONDS` | `8` | SPA render-stability wait cap. |
| `ARGUS_DEFAULT_FINGERPRINT_OS` | `linux` | Camoufox fingerprint OS (keep `linux` with the provided Dockerfile). |
| `ARGUS_AI_BASE_URL` | *(empty)* | OpenAI-compatible base URL for the `/v1/extract-price` AI fallback (no trailing `/chat/completions`). |
| `ARGUS_AI_API_KEY` | *(empty)* | Provider API key (**secret** — never logged). |
| `ARGUS_AI_MODEL` | *(empty)* | Chat model id, e.g. a DeepSeek free-tier model. |
| `ARGUS_AI_ZEN_HOST` | *(empty)* | Optional: when the base URL's host matches, Zen parity headers below ride on AI requests. |
| `ARGUS_AI_USER_AGENT` / `ARGUS_AI_CLIENT_HEADER` | *(empty)* | The Zen parity header values (`User-Agent` / `X-Opencode-Client`). |
| `ARGUS_AI_CONCURRENCY` | `1` | Max concurrent LLM calls (free tiers 429 under bursts). |
| `ARGUS_AI_MIN_INTERVAL_MS` | `200` | Minimum gap between LLM calls (live-tunable). |
| `ARGUS_AI_MAX_RETRIES` | `3` | Retries for transient provider errors (429/502/503/504 only). |

All three of `ARGUS_AI_BASE_URL` + `ARGUS_AI_API_KEY` + `ARGUS_AI_MODEL` must
be set for the AI stage to exist; leave any empty and `/v1/extract-price`
behaves as a deterministic JSON-LD-only service.

## Deploying alongside iris (the motivating client)

Iris's `fetchPage` / `extract-image` become thin clients of argus:

```ts
const response = await fetch(`${ARGUS_BASE_URL}/v1/fetch`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    authorization: `Bearer ${ARGUS_API_TOKEN}`,
  },
  body: JSON.stringify({ url, detectBlocked: true }),
  signal: AbortSignal.timeout(45_000),
});
```

Retry / backoff / concurrency limiting stays in the caller; argus is only the
fetch transport. The iris-side migration is tracked as iris task
`08-20-migrate-camoufox-to-argus`.

## Project layout

```
src/argus/
├── main.py           # FastAPI app + lifespan
├── config.py         # pydantic-settings (all ARGUS_* env)
├── auth.py           # bearer-token dependency (constant-time compare)
├── browser.py        # shared Camoufox: lazy launch, idle teardown, single-flight
├── render.py         # SPA render-wait + resilient content snapshot
├── signatures.py     # anti-bot blocked-page registry (pluggable)
├── diagnostics.py    # consecutive-failure tracking + degraded-browser logs
├── schemas.py        # request/response models (incl. the Cookie shape)
├── cookies.py        # cookie normalization to the add_cookies shape
└── routes/
    ├── health.py     # GET /health
    ├── fetch.py      # POST /v1/fetch
    └── fetch_image.py# POST /v1/fetch-image
```

Deferred work (persistent login sessions, per-session proxies, per-token rate
limiting, metrics): [`docs/future.md`](docs/future.md).
