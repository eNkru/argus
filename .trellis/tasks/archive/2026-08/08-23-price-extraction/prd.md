# Add price extraction to argus (JSON-LD first, AI fallback)

## Goal

Move product-price extraction **into argus** so a caller can get a parsed price
(+ full JSON-LD) from a single endpoint call, instead of fetching HTML from
argus and running extraction in the caller (as iris does today). Two stages:

1. **Deterministic (cheap, no LLM):** parse the page's JSON-LD
   `<script type="application/ld+json">` for a schema.org `Offer` /
   `AggregateOffer` with a sane price (`> 0`); return `{price, currency,
   availability, name}` **plus the full primary Product node** as `jsonld`.
2. **AI fallback (expensive, LLM):** when stage 1 finds no usable price and
   the caller hasn't disabled it, reduce the HTML and call an
   OpenAI-compatible LLM to extract `{price, currency, name, available}`.

## User value

A caller doing price tracking (iris or any other) no longer reimplements
JSON-LD parsing + AI extraction: one `POST /v1/extract-price` call returns a
parsed price, currency, availability, name, and the rich structured Product
node. The deterministic JSON-LD path is free (no LLM cost/latency) and works for
the common case; the AI path covers sites without JSON-LD.

## Background (verified 2026-08-23)

### Three target retailers — all expose JSON-LD `Offer`
Live argus dev-server fetches confirmed all three emit a schema.org `Offer`
with `price` + `priceCurrency` + `availability`. A single recursive extractor
(walk all JSON-LD blocks, find first node with `price`+`priceCurrency`,
normalize via `Decimal`, sanity-check `> 0`) returns the correct price for all
three with **zero per-site selectors**:

| Site | `Offer.price` | Currency | Availability | Live pass rate (sample) |
|------|---------------|----------|--------------|--------------------------|
| pbtech | 499 (int) | NZD | InStock | 1/1 |
| kogan | "264" (str) | NZD | InStock | 11/11 |
| farmers | "599.99" (str) | NZD | InStock (+ non-standard `original_price:"1199.99"`) | 16/22 (~73%); 6/22 empty-shell block already classified `akamai-empty-shell`, `retryable:true` |

The user's original "price missing" / "page is protected" reports were
consumer-side (pbtech: parsed the empty `.priceClass-pb` span instead of
JSON-LD; farmers: the `{ok:false,reason:"blocked"}` empty response was
misread). No argus bug — but the experience motivates moving extraction into
argus so callers get a parsed price directly.

### Iris already implements the AI half (caller-side)
`packages/prices/src/pipeline/ai-extract.ts` does AI-only extraction (no
JSON-LD-first stage). Porting it into argus **and adding the JSON-LD stage** is
an improvement over iris's approach (cheaper + more reliable for the common
case). Patterns to mirror: `reducePageHtml` (visible text ≤8KB + ≤3
price-bearing script blobs ≤3KB), single `generateText` call (avoids DeepSeek
thinking-mode multi-step `reasoning_content` drop, iris §1e), OpenAI-compatible
provider (Zen/DeepSeek free tier), throttle (concurrency + min-interval gap),
retry on 429/502/503/504, never-throws (missing key → logged no-op → null),
short-circuit AI on blocked pages. Output schema (`types.ts`):
`{available:true, price>0, currency, name?}` ∪ `{available:false, ...nulls}`.

### Argus architecture boundary (the key tension)
`docs/architecture.md` currently says "AI extraction — out of scope" / "What
stays in the caller: AI extraction." This task **reverses that boundary**:
argus gains an LLM HTTP client, an API-key secret (its first non-token
secret), prompt design, token cost + seconds of latency on a request, and a
new external dep. The user has confirmed intent to proceed (acknowledged the
boundary before choosing the API surface). `architecture.md` will be updated
to document the new boundary (R6) — `/v1/fetch` stays pure transport; argus
gains an extraction **sibling** endpoint. Callers still own cross-product
retry/backoff/pLimit and price-history storage (those do not move).

## Requirements

- **R1 — JSON-LD extraction.** Parse all JSON-LD blocks; locate the primary
  `Offer` (or `AggregateOffer.lowPrice`) with `price > 0`; return
  `{price, currency, availability, name}` + the full primary Product node.
  Tolerate int/str prices and concatenated-objects-without-separator.
- **R2 — AI fallback.** When R1 finds no usable price and `aiFallback` is
  true and the AI client is configured, reduce the HTML and call an
  OpenAI-compatible LLM to extract `{price, currency, name, available}`.
  Never throws; failures map to `extraction_failed`. No AI call on blocked
  pages.
- **R3 — API surface (decided).** New endpoint `POST /v1/extract-price`,
  separate from `/v1/fetch`. Request mirrors `FetchRequest` fetch knobs plus
  `aiFallback: bool = true`. `/v1/fetch` response shape is unchanged
  (backward compatible).
- **R4 — Config & secrets.** New `ARGUS_AI_*` env vars (base URL, API key,
  model, optional Zen headers, concurrency=1, min-interval=200ms, max-retries=3)
  in `config.py`. `.env.example` + README + `api-spec.md` updated. API key
  never logged (`logging-guidelines.md`).
- **R5 — No persistence.** No DB, no price/HTML cache. State remains
  per-request (`database-guidelines.md`).
- **R6 — Contract docs.** `api-spec.md` (new path + `extraction_failed`
  reason), `architecture.md` (boundary reversal), README config table,
  `.env.example`.
- **R7 — Tests.** `jsonld` (int/str price, nested Product, multi-block,
  AggregateOffer, price=0 guard, `original_price`, empty shell, concat-objects).
  `ai` (mocked httpx: happy/429-retry/503-retry/400-no-retry/missing-key/
  schema-fail). `extract` route (JSON-LD hit, blocked short-circuit,
  aiFallback=false → `extraction_failed`, auth).

## Key decisions

- **D1 (Q1) — API surface: new `POST /v1/extract-price` endpoint.** (User
  chose A.) Keeps `/v1/fetch` pure transport; makes AI fallback opt-in by
  construction; mirrors iris's fetch/extract split with only the location of
  the extract half moving into argus.
- **D2 (Q2) — AI provider: generic OpenAI-compatible via raw `httpx`.** No
  `openai` SDK (lean deps, full retry/throttle control, matches argus
  philosophy). Config shape mirrors iris so operators point both at one
  provider.
- **D3 (Q3) — `aiFallback: bool = true` request flag.** Default matches the
  user's described behavior (JSON-LD → AI). The flag lets callers guarantee
  deterministic-only (no LLM cost/latency). When false and no JSON-LD price →
  `extraction_failed`.
- **D4 (Q4) — `jsonld` response field = the primary Product node.** *(Confirmed by user, 2026-08-23: "rich data, no bloat".)* Carries the Offer + name/brand/gtin/reviews — the rich structured data the user wants returned. Other JSON-LD blocks (BreadcrumbList/WebSite) are not returned (they carry no product/price data and would bloat the response). If the Offer is not nested in a Product, return the Offer node itself.
- **D5 (Q5) — AI output matches iris `{price, currency, name, available}`.**
  Pydantic-validated, discriminated on `available` (price>0 required when
  true). Nearly free to include; matches iris's proven prompt.
- **D6 (Q6) — AI fallback trigger: any "no usable Offer price".** Covers:
  no JSON-LD at all, JSON-LD present but no Offer, `price <= 0`, only an
  `AggregateOffer` (use `lowPrice` first; if absent, fall back to AI).

## Acceptance criteria

- **AC1.** `POST /v1/extract-price` against a pbtech/kogan/farmers PDP URL
  returns the correct `{price, currency, availability}` sourced from JSON-LD,
  **without** invoking the LLM, and includes the full `jsonld` Product node.
- **AC2.** A page with no JSON-LD price triggers the AI fallback and returns
  `{price, currency, name, available}` with `source:"ai"`, `jsonld:null`
  (provider mocked in tests; live free-tier smoke when `ARGUS_AI_*` set).
- **AC3.** A blocked page (farmers empty-shell) returns
  `{ok:false, reason:"blocked", signature, retryable}` and **does not** call
  the LLM.
- **AC4.** Missing AI config (empty key) → JSON-LD path still works
  (AC1 holds); AI fallback degrades to a logged no-op → `extraction_failed`,
  never throws.
- **AC5.** `docs/api-spec.md` + `docs/architecture.md` + README config table
  + `.env.example` reflect the new endpoint + `ARGUS_AI_*` vars.
- **AC6.** `pytest -q` green (existing 37 + new jsonld/ai/extract tests; no
  browser/network needed — pure modules + mocked httpx + `SimpleNamespace`
  app.state per `quality-guidelines.md`).

## Out of scope (MVP)

- **No persistent storage** of prices / history / fetch cache (R5). History
  stays in the caller.
- **No scheduler / cross-product retry-orchestration loop.** Argus does one
  fetch+extract per request; the caller owns backoff/pLimit (iris
  `fetch-page.ts` already has this).
- **No correction of stale-but-present JSON-LD prices.** If a site ships a
  wrong JSON-LD price, AI fallback will NOT fire (price is present). Deferred
  risk; out of MVP.
- **No image extraction / download.** (iris has a separate `extract-image`
  pipeline; not part of this task.)
- **No per-session/proxy/rate-limit features** (already on `docs/future.md`).
- **No iris-side migration.** Iris wiring to call `/v1/extract-price` instead
  of `fetchPage`+`aiExtractPrice` is a follow-up, not this task.
