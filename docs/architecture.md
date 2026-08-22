# Argus architecture

Argus is a general-purpose, Camoufox-backed anti-detect browser fetch service.
It holds one shared `AsyncCamoufox` browser (an engine-level anti-detect Firefox
fork) and exposes a small HTTP API for fetching page HTML and binary images
through it. The engine lifecycle, render-wait, and content-snapshot logic are
ported from the iris sidecar; the boundaries are hardened for multi-client use.

## Layering: Camoufox + Playwright API

```
your code
   │  calls
   ▼
camoufox.async_api.AsyncCamoufox   ← anti-detect engine launcher (Firefox fork + fingerprint spoofing)
   │  launches and yields
   ▼
Playwright Browser / BrowserContext ← the automation API (goto, content, cookies, evaluate)
   │  drives
   ▼
Camoufox Firefox binary            ← the actual browser process
```

Camoufox is the browser. Playwright is the remote-control API that drives it.
`AsyncCamoufox.__aenter__()` yields a Playwright `Browser`; calls like
`page.goto()`, `page.content()`, `context.add_cookies()` are Playwright API
calls on that yielded object. **There is no Playwright Chromium in this stack.**

## Browser lifecycle (lazy, single-flight, idle-teardown)

The shared browser is **not** launched at boot. It launches lazily on the first
fetch (`BrowserManager.ensure_browser`) under a single-flight lock, so N
concurrent first-fetches produce exactly one launch. After an idle timeout
(`ARGUS_IDLE_TIMEOUT_SECONDS`, default 300s) with no fetch in flight, the
browser tears down to reclaim the ~350-500 MB Firefox process tree. An in-flight
counter prevents tearing down a browser mid-navigation.

This keeps the resident process absent between scrapes on lightly-loaded hosts.

## Per-request ephemeral context

Every fetch creates a fresh `BrowserContext` (`browser.new_context(...)`),
optionally injects caller-supplied cookies, navigates, snapshots, then closes
the context in a `finally`. The shared browser is one process; the context is
the isolated cookie jar. This guarantees no cookie/storage leak between callers
or between retailers.

- **Login-free site:** `POST /v1/fetch {url}` — fresh empty context, no cookies.
- **Login-gated site (e.g. Taobao):** `POST /v1/fetch {url, cookies}` — caller
  passes their logged-in cookie jar; the context carries those cookies for the
  one navigation, then is destroyed.

## Optional per-request overrides

`/v1/fetch` and `/v1/fetch-image` accept optional `locale` and `userAgent`
fields, applied at context creation (`new_context(locale=..., user_agent=...)`).

**Fingerprint caveat for `userAgent`:** Camoufox spoofs `navigator.userAgent`
at the engine level as part of its anti-detect fingerprint. Overriding the UA
at the context level can create an inconsistency between the spoofed engine
fingerprint and the declared UA — a fingerprinting tell. Prefer to leave
`userAgent` unset and rely on Camoufox's default; set it only when you have a
specific reason (e.g. matching the UA a cookie jar was issued under, for a
login-gated site that ties the session to a UA). `locale` is safe to override
freely (it only affects `Accept-Language` / `navigator.language`).

## Bearer-token auth

All `/v1/*` routes require `Authorization: Bearer <token>`. Valid tokens are the
comma-separated values in `ARGUS_API_TOKENS` (supports rotation + per-client
issuance). Comparison uses `secrets.compare_digest` (constant-time). `/health`
is open for readiness probes. `ARGUS_AUTH_DISABLED=true` bypasses auth for local
dev.

## Blocked-signature registry

When a WAF blocks the fetch, the browser still returns 200 OK with HTML — it's
just the *wrong* HTML (a captcha / challenge / deny page). `signatures.py`
ships a generic default registry (cloudflare-just-a-moment, akamai-deny,
datadome-captcha, empty-shell) that classifies such pages so the caller gets an
explicit `{ok:false, reason:"blocked", signature:"..."}` instead of garbage
HTML masquerading as content.

Per-request `detectBlocked: false` (default `true`) opts out — the raw HTML is
returned as `{ok:true, html}` so a caller with a richer registry can classify
itself. The registry is pluggable (`DEFAULT_REGISTRY` is a plain list) so
signatures can be extended without touching the fetch path.

## Price extraction (JSON-LD-first, AI fallback)

Since 2026-08-23 argus also offers ``POST /v1/extract-price`` — an extraction
sibling to ``/v1/fetch``. It navigates through the same shared browser
pipeline, then runs two stages on the rendered HTML:

1. **Deterministic JSON-LD parse** (``jsonld.py``, no LLM): schema.org
   Offer/AggregateOffer with a usable price (> 0). Verified live against
   pbtech/kogan/farmers PDPs — zero per-site selectors. On a hit the caller
   gets ``{price, currency, availability, name}`` plus the full primary
   Product node as ``jsonld``.
2. **AI fallback** (``ai.py``): only when stage 1 misses and the caller hasn't
   sent ``aiFallback:false`` — an OpenAI-compatible LLM (raw httpx, no SDK)
   reads a reduced page and returns ``{price, currency, name, available}``.
   Configured via ``ARGUS_AI_*``; unconfigured/degraded AI maps to
   ``extraction_failed``, never a 500. Blocked WAF pages short-circuit before
   any LLM call.

The boundary shift: AI extraction moved from "stays in the caller" into argus.
What did NOT move: cross-product retry/backoff/pLimit orchestration and price-
history storage stay caller-side; ``/v1/fetch`` remains pure transport and its
response shape is unchanged.

## What stays in the caller (not argus)

- Retry / backoff / pLimit across products — app-level orchestration.
- Retailer-specific signature registries — app-specific.
- Image magic-byte validation — the caller validates the bytes argus returns.
- Price-history storage and alerting — the caller records readings; argus is
  stateless per request.

Argus is the **fetch transport**. The orchestration layer lives in the caller
(e.g. iris's `fetch-page.ts`), now pointed at argus with a one-line base-URL +
bearer-token change.

## Diagnostics

A `FailureTracker` counts consecutive fetch failures and, exactly once when the
count crosses a threshold (3), emits a richer "browser degraded" summary with the
traceback — so a degrading browser reads as a trend in the logs, not isolated
per-request warnings. The counter resets on any success.
