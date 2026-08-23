# Design — Add price extraction to argus

> Decision Q1 (API surface) resolved by user: **(A) new endpoint
> `POST /v1/extract-price`**. Q2–Q6 resolve by recommendation below, consistent
> with iris's proven AI-extraction approach + the user's "return the full
> json-ld to caller" wording.

## Architecture & boundaries

Argus gains an **extraction sibling** to `/v1/fetch`. `/v1/fetch` stays the pure
transport (response shape unchanged — backward compatible). The new
`POST /v1/extract-price` endpoint fetches a page through the **existing**
`BrowserManager` + `render.py` + `signatures.py` pipeline (no second navigation
engine), then runs a two-stage extractor on the HTML:

```
POST /v1/extract-price {url, ...fetch knobs, aiFallback=true}
   │
   ▼
routes/extract.py  (thin: validate → delegate → map to response model)
   │
   ├── BrowserManager.ensure_browser() → fresh ephemeral BrowserContext
   │      (reuses routes/fetch.py navigation path: goto, wait_for_render,
   │       snapshot_content)
   │
   ├── if signatures.detect(html) → {ok:false, reason:"blocked", signature, retryable}   (NO LLM call — mirrors iris checkPrice)
   │
   ├── jsonld.extract_offer(html) → {price, currency, availability, name, product_node}   (deterministic, no LLM)
   │      if usable price (price>0) → {ok:true, source:"jsonld", jsonld:product_node, ...}
   │
   └── if no usable price AND request.aiFallback AND ai client configured:
          ai.extract_price(html, url) → {price, currency, name, available}   (LLM)
             → {ok:true, source:"ai", jsonld:null, ...}  OR  {ok:false, reason:"extraction_failed"}
       else → {ok:false, reason:"extraction_failed"}
```

### Module layout (follows `directory-structure.md`: flat single package, one concern per file, routes one file per endpoint group)

```
src/argus/
├── jsonld.py          # NEW — JSON-LD block parser + Offer/AggregateOffer extractor (pure, no browser)
├── ai.py              # NEW — OpenAI-compatible httpx client + reduce_html + prompt + throttle/retry (never throws)
├── routes/extract.py  # NEW — POST /v1/extract-price (thin, router-level auth)
├── schemas.py         # + ExtractPriceRequest, ExtractPriceResponseOk/Fail
├── config.py          # + ARGUS_AI_* settings
└── main.py            # lifespan: construct AiClient singleton (None if unconfigured) on app.state; include extract.router
```

The navigation+render+blocked-classification half is **shared, not
duplicated**: `routes/extract.py` calls `BrowserManager` /
`wait_for_render` / `snapshot_content` / `signatures.detect` exactly as
`routes/fetch.py` does. To avoid copy-pasting the 40-line fetch-or-block
sequence across two routes, factor the shared navigation into a small
`render.py` (or new `navigate.py`) helper `_fetch_html(browser_manager,
request) -> HtmlResult` used by both routes. (Refactor is mechanical and
covered by existing fetch tests + new extract tests.)

## Contracts

### Request — `ExtractPriceRequest`

Mirrors `FetchRequest` (same fetch knobs — it navigates internally) **plus**
`aiFallback`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | str (absolute http(s)) | required | same validator as `FetchRequest` |
| `cookies` | list[Cookie] \| None | None | login-gated sites |
| `waitUntil` | `domcontentloaded`\|`load`\|`networkidle` | `domcontentloaded` | |
| `renderWaitMs` | int | 8000 | |
| `timeoutMs` | int | 35000 | |
| `detectBlocked` | bool | true | run `signatures.py` before extraction |
| `locale` | str \| None | None | |
| `userAgent` | str \| None | None | fingerprint caveat (architecture.md) |
| **`aiFallback`** | bool | **true** | false = JSON-LD-only (deterministic, no LLM cost/latency) |

camelCase JSON keys (matches existing public-API convention, `directory-structure.md`).

### Response — discriminated on `ok`

```python
class ExtractPriceResponseOk(BaseModel):
    ok: bool = True
    source: Literal["jsonld", "ai"]
    url: str                       # final URL after redirects (parity with /v1/fetch)
    available: bool                # InStock/BackOrder/PreOrder → true; OutOfStock/Discontinued → false
    price: str | None              # Decimal-normalized string e.g. "599.99"; None when not available
    currency: str | None           # ISO 4217 e.g. "NZD"
    availability: str | None       # schema.org URI (jsonld source); None for ai source
    name: str | None
    jsonld: dict | None            # full primary Product node (source="jsonld" only); null for source="ai"

class ExtractPriceResponseFail(BaseModel):
    ok: bool = False
    reason: Literal["blocked", "fetch_failed", "extraction_failed"]
    signature: str | None = None   # present only when reason="blocked"
    retryable: bool | None = None   # present only when reason="blocked"
```

- `price` as **Decimal-string** (not float): avoids binary-rounding in JSON
  (mirrors iris `price.toFixed(2)`). Normalize `499` (int) and `"599.99"` (str)
  via `Decimal(str(v)).quantize(Decimal("0.01"))`.
- `available` for JSON-LD source: `True` unless `availability` explicitly is
  `https://schema.org/OutOfStock` / `Discontinued`. Absent availability + price
  present → `True`. For AI source: the model's verdict.
- `jsonld` field = the **primary Product node** (the dict containing the
  `Offer` + `name`/`brand`/`gtin`/`review` etc.) — this is "the full json-ld"
  the user asked to return. Other JSON-LD blocks on the page (WebSite,
  BreadcrumbList) are not returned (they carry no product/price data). If the
  Offer is not nested in a Product, return the Offer node itself.
- New reason `extraction_failed` is a **closed-Literal contract change** → must
  update `docs/api-spec.md` (R6).

### Deterministic extractor (`jsonld.py`) — acceptance

- Parse all `<script type="application/ld+json">` blocks; tolerate the
  concatenated-object-without-separator case (`}{` → `},{`, wrap in `[]`).
- Recursively walk for the first node that is `@type=="Offer"` (or carries
  `price`+`priceCurrency`), OR `@type=="AggregateOffer"` (use `lowPrice`).
- Sanity: `price = Decimal(str(price).replace(",",""))` ; skip if `<= 0`.
- Non-standard extras surfaced when present: farmers' `original_price` is part
  of the returned `jsonld` node (no special handling — it rides along in the
  dict).

### AI fallback (`ai.py`) — ported from iris `ai-extract.ts`

- **Provider:** generic OpenAI-compatible `POST {base_url}/chat/completions`
  via `httpx.AsyncClient`. **No `openai` SDK** — raw httpx keeps the new-dep
  footprint minimal and matches argus's lean-deps philosophy. `httpx` is
  promoted from dev to runtime deps in `pyproject.toml`.
- **`reduce_html(html)`**: visible text (strip scripts/styles/tags, collapse
  whitespace) ≤8KB + ≤3 `<script>` blobs matching `price|currency|amount|offers`
  ≤3KB each. Ported verbatim from iris `reducePageHtml`.
- **Prompt:** single `generateText`-equivalent (one chat-completions call, no
  tool loop) — avoids iris's DeepSeek thinking-mode multi-step failure
  (`reasoning_content` drop, §1e). The prompt instructs: return ONLY a JSON
  object `{"price":number>0,"currency":str,"name":str?,"available":bool}` or
  `{"available":false, ...nulls}`.
- **Output validation:** pydantic model mirroring iris `priceExtractionSchema`
  (discriminated on `available`: when true, `price>0`+`currency` required).
  Parse `{`..`}` slice from response text (tolerates prose/fences) → validate.
- **Throttle:** `asyncio.Semaphore(ARGUS_AI_CONCURRENCY)` + module-level
  `last_call_ended_at` + `asyncio.sleep(min_interval_gap)`. Read concurrency
  once at first use (boot-time), min-interval live-tunable — mirrors iris.
- **Retry:** 429/502/503/504 → exponential backoff `2**attempt * 1000 + rand`
  ms, max 3 attempts; 400/other → no retry (mirrors iris `isRetryableError`).
- **Never throws:** missing key → logged no-op → returns `None` (caller maps
  to `extraction_failed`). Mirrors iris `aiExtractPrice` + `error-handling.md`.
- **Singleton:** `AiClient` constructed in `main.lifespan` on `app.state.ai_client`
  (None when `ARGUS_AI_API_KEY` empty) — same pattern as `BrowserManager`.

## Config & secrets (R4)

New `ARGUS_AI_*` settings in `config.py` (pydantic-settings, `ARGUS_` prefix —
already the convention). Mirrors iris env shape so an operator can point both
services at the same provider:

| Env var | Default | Notes |
|---------|---------|-------|
| `ARGUS_AI_BASE_URL` | `""` | OpenAI-compatible base, e.g. `https://opencode.ai/zen/v1` |
| `ARGUS_AI_API_KEY` | `""` | **secret** — empty → AI degrades to no-op (AC4). First non-token secret in argus. |
| `ARGUS_AI_MODEL` | `""` | e.g. `deepseek-v4-flash-free` |
| `ARGUS_AI_ZEN_HOST` | `""` | optional; when `URL(base_url).host==zen_host`, send the Zen headers |
| `ARGUS_AI_USER_AGENT` | `""` | optional Zen header |
| `ARGUS_AI_CLIENT_HEADER` | `""` | optional Zen `X-Opencode-Client` header |
| `ARGUS_AI_CONCURRENCY` | `1` | conservative, mirror iris default |
| `ARGUS_AI_MIN_INTERVAL_MS` | `200` | gap between LLM calls |
| `ARGUS_AI_MAX_RETRIES` | `3` | 429/502/503/504 only |

Secrets handling per `logging-guidelines.md`: the API key is **never logged**
(log "AI provider configured" / "AI provider not configured (missing key)" —
no value). `.env.example` gains a commented `# AI price-extraction fallback`
block; `.gitignore` already covers `.env`.

## Compatibility & migration (the boundary reversal — R6)

This is the documented-scope change. `docs/architecture.md` currently says
"AI extraction — out of scope" / "What stays in the caller: AI extraction."
After this task:

- `architecture.md` → move "AI extraction" from "stays in caller" to a new
  "**Price extraction** (JSON-LD-first, AI fallback)" section describing the
  new endpoint; state explicitly that `/v1/fetch` remains pure transport and
  callers may use either (fetch HTML themselves, or call extract for a parsed
  price). Callers still own retry/backoff/pLimit across products and price
  history storage — that does NOT move.
- `api-spec.md` → add the `/v1/extract-price` path + `ExtractPriceRequest` /
  response schemas + the new `extraction_failed` reason.
- `future.md` → no change (this is not deferred work).
- `database-guidelines.md` → no change (still no DB; extraction is
  per-request, stateless).

No breaking change to `/v1/fetch` or `/v1/fetch-image`. The new endpoint is
additive. Iris can migrate `checkPrice` to call `/v1/extract-price` instead of
`fetchPage`+`aiExtractPrice` — but that migration is **iris's concern, out of
this task's scope** (this task delivers the argus side; iris wiring is a
follow-up).

## Tradeoffs

- **Raw httpx vs `openai` SDK.** Raw httpx = ~0 new heavy deps, full control
  over retry/throttle (mirrors iris's custom loop), but must hand-write the
  chat-completions request + JSON parsing. `openai` SDK = batteries included
  but a heavier dep and opaque retry. **Decision: raw httpx** — matches
  argus's lean-deps philosophy; the chat-completions API is simple.
- **`aiFallback` flag default true.** Matches user's described behavior
  (JSON-LD→AI). The flag exists so callers can guarantee deterministic-only
  (no LLM cost/latency). Alternative (no flag, always-AI-when-absent) would
  force LLM cost on every caller even when they only want the JSON-LD path.
- **`jsonld` field = primary Product node only.** Literally "the full json-ld"
  could mean "all blocks." Decision: return the Product node (where the price +
  rich data live); BreadcrumbList/WebSite blocks add no product value and
  bloat the response. Flagged for user review — if they want all raw blocks,
  trivial to widen.
- **`source="ai"` returns `jsonld:null`.** The AI path has no JSON-LD to return
  (that's why it fell back). Caller gets `{price, currency, name, available}`
  only.
- **Latency/cost on a fetch slot.** AI fallback adds seconds + tokens to a
  request holding one `ARGUS_CONCURRENCY` slot. Mitigated by: (a) default
  `ARGUS_AI_CONCURRENCY=1` (separate from browser concurrency, so LLM calls
  don't starve fetches), (b) `aiFallback:false` opt-out, (c) blocked pages
  short-circuit before any LLM call.

## Operational / rollback

- **Rollback point:** the new endpoint is additive — disabling it = remove
  `app.include_router(extract.router)` from `main.py` (or set
  `ARGUS_AI_*` empty to neuter the fallback). No data migration, no DB.
- **Monitoring:** reuse `FailureTracker` pattern — AI failures counted; a
  consecutive-AI-failure threshold log could be added later (out of MVP).
- **Smoke test (manual, AC1/AC2):** the `./dev.sh` server + the three saved
  `/tmp/*.json` responses (pbtech/kogan/farmers) validate the JSON-LD path
  offline-style; a live farmers fetch validates the blocked short-circuit; a
  configured `ARGUS_AI_*` against a no-JSON-LD page validates the AI path.
