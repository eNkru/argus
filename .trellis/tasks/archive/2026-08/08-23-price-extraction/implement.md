# Implement — Add price extraction to argus

> Complex task → `prd.md` + `design.md` + this `implement.md` required before
> `task.py start` (workflow Phase 1.4). Two phases so the deterministic path
> is independently verifiable **before** the LLM half lands.
>
> **Progress:** Phase A steps 1–8 COMPLETE + committed (ad7a918, 2026-08-23).
> Phase B steps 9–17 COMPLETE (2026-08-23). Note: both sub-agent dispatches
> terminated early on provider errors/stops; Phase A was implemented inline by
> the main session, Phase B was implemented by a sub-agent up to step 14 with
> the main session finishing tests/docs. Suite at Gate B: 85 passed
> (65 at Gate A + 18 test_ai + 2 source=ai route tests). Live regression:
> /health ✓, /v1/fetch ✓ post-refactor, extract-price degrades to
> extraction_failed with unconfigured AI ✓ (AC4). A live AI smoke requires
> ARGUS_AI_* credentials — left to the operator.

## Phase A — JSON-LD extraction endpoint (shippable alone) ✅

1. **`src/argus/jsonld.py`** — pure module, no browser.
   - `_LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL)`
   - `_parse_blocks(html)` → list of parsed JSON objects (tolerate `}{`→`},{` wrap in `[]`).
   - `_walk(node)` generator yielding any dict with `@type=="Offer"` or `@type=="AggregateOffer"` or (`price`+`priceCurrency`).
   - `extract_offer(html) -> OfferExtract | None` where `OfferExtract = {price: Decimal, currency: str|None, availability: str|None, name: str|None, product_node: dict|None}`. Use `AggregateOffer.lowPrice` when no `Offer.price`. Normalize price via `Decimal(str(v).replace(",",""))`; skip `<=0`.
   - `available_from(availability: str|None) -> bool`: `True` unless ends with `OutOfStock`/`Discontinued`.
   - Module docstring cites the 2026-08-23 evidence (pbtech int 499, kogan/farmers str, farmers `original_price`).
   - Validation: `pytest tests/test_jsonld.py` (offline).

2. **`src/argus/schemas.py`** — add `ExtractPriceRequest` (mirror `FetchRequest` fields + `aiFallback: bool = True`) and `ExtractPriceResponseOk` / `ExtractPriceResponseFail` per `design.md`. New `reason` Literal adds `"extraction_failed"`.

3. **`src/argus/routes/extract.py`** — thin route:
   - `router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])` (router-level auth, matches `routes/fetch.py`).
   - Handler: validate → fetch HTML via shared helper → `signatures.detect` (blocked short-circuit, no LLM) → `jsonld.extract_offer` → map to `ExtractPriceResponseOk(source="jsonld", jsonld=product_node, ...)`. If no JSON-LD price and `aiFallback=false` → `ExtractPriceResponseFail(reason="extraction_failed")`. AI path stubbed (raises NotImplementedError / returns `extraction_failed`) until Phase B.
   - **Refactor:** extract the shared fetch+render+blocked-classify sequence from `routes/fetch.py` into a helper (e.g. `render.py::fetch_html_or_blocked(browser_manager, request) -> Ok|Blocked|Failed`) and call from **both** routes. Keep `routes/fetch.py` behavior identical (existing fetch tests must still pass).

4. **`src/argus/main.py`** — `from .routes import extract` + `app.include_router(extract.router)`. (No `app.state.ai_client` yet — that's Phase B.)

5. **`docs/api-spec.md`** — add `/v1/extract-price` path + request/response schemas (OpenAPI yaml block, matching existing style) + the `extraction_failed` reason.

6. **Tests — `tests/test_jsonld.py`** (pure, offline): int price (pbtech shape), str price (kogan/farmers), nested-in-Product vs standalone Offer, multi-block (only one has Offer), AggregateOffer lowPrice, price=0/missing → None, farmers `original_price` rides in `product_node`, concatenated-objects-without-separator parse, `<title>`/`<body>` absent (empty shell → None). Assert `Decimal` price values.

7. **Tests — `tests/test_extract_route.py`** (offline, fake `app.state` per `quality-guidelines.md`): JSON-LD-hit returns `source:"jsonld"` + full `jsonld` node; blocked HTML returns `{ok:false,reason:"blocked",signature,retryable}` (no LLM); no-JSON-LD + `aiFallback:false` → `reason:"extraction_failed"`; auth 401 without token. Use `SimpleNamespace` fake browser manager + a small FastAPI app + `TestClient`, mirroring `tests/test_health.py::_make_app`.

8. **Gate A:** `source .venv/bin/activate && pytest -q` — full suite green (existing 37 + new jsonld + new route tests). Manual smoke: `./dev.sh` then `POST /v1/extract-price` against pbtech/kogan/farmers URLs → expect `source:"jsonld"` with correct prices (reuse the saved `/tmp/*.json` to compare).

## Phase B — AI fallback ✅

> Design note added during Phase A: the extract route already reads
> `getattr(req.app.state, "ai_client", None)` and degrades to a logged no-op,
> so Phase B only needs steps 9–12 + 14–16 plus replacing the route's tail
> (step 13) with the real `ai_client.extract_price(...)` call.

9. **`pyproject.toml`** — move `httpx>=0.27` from `[project.optional-dependencies].dev` to `[project].dependencies` (it's now runtime). Re-run `pip install -e ".[dev]"`.

10. **`src/argus/config.py`** — add `ARGUS_AI_*` fields per `design.md` table (`ai_base_url`, `ai_api_key`, `ai_model`, `ai_zen_host`, `ai_user_agent`, `ai_client_header`, `ai_concurrency=1`, `ai_min_interval_ms=200`, `ai_max_retries=3`).

11. **`src/argus/ai.py`** — port iris `ai-extract.ts`:
    - `AiClient` class (constructed in lifespan): holds `httpx.AsyncClient`, resolved config; `None`-able when key empty.
    - `reduce_html(html) -> str` (visible text ≤8KB + ≤3 price-bearing script blobs ≤3KB).
    - `_build_prompt(url, page_content) -> str`.
    - `_parse_extraction(text) -> PriceExtraction|None` (slice `{..}`, pydantic-validate; discriminated on `available`).
    - `_is_retryable(exc) -> bool` (429/502/503/504 + message regex).
    - `extract_price(self, html, url) -> PriceExtraction|None`: semaphore + min-interval gap + retry loop, `max_retries`, never throws (logged no-op → None).
    - Zen-header logic when `URL(base_url).host == zen_host`.
    - Module docstring cites iris §1e (DeepSeek multi-step failure → single-call) + 2026-08-19 503 incident.
    - Validation: `pytest tests/test_ai.py` (httpx mocked via `respx` or monkeypatch; offline).

12. **`src/argus/main.py`** — lifespan constructs `AiClient(settings)` on `app.state.ai_client` (None if key empty); close on shutdown.

13. **`src/argus/routes/extract.py`** — wire Phase-B arm: when `jsonld.extract_offer` returns None AND `request.aiFallback` AND `app.state.ai_client` is not None → `await ai_client.extract_price(html, request.url)` → map to `ExtractPriceResponseOk(source="ai", jsonld=None, ...)`; if AI returns None → `reason:"extraction_failed"`. If `aiFallback` true but `ai_client is None` (key empty) → log "AI provider not configured" → `reason:"extraction_failed"` (AC4 degrade-to-no-op).

14. **`.env.example`** — append commented `# AI price-extraction fallback` block with all `ARGUS_AI_*` vars.

15. **`docs/architecture.md`** — move "AI extraction" from "stays in caller" to a new "Price extraction" section; state `/v1/fetch` stays pure transport; caller still owns retry/backoff/pLimit/history.

16. **Tests — `tests/test_ai.py`** (offline, mocked httpx): happy path returns `{price,currency,name,available}`; 429-then-200 retry; 503-then-200 retry; 400 → no retry → None; missing key → `AiClient` is None; schema-validation failure → None; prose-around-JSON parsed. Use `respx` or a fake transport — no network.

17. **Gate B (final check):** `pytest -q` green. Manual smoke with a configured `ARGUS_AI_*` (point at the same Zen/DeepSeek free tier iris uses) against a deliberately JSON-LD-less page (or `detectBlocked:false` on a known no-jsonld URL) → expect `source:"ai"`.

## Validation commands

```bash
source .venv/bin/activate
pytest -q                       # full suite (existing + new), no browser/network needed
# manual smoke (Phase A):
./dev.sh
TOKEN=$(grep ^ARGUS_API_TOKENS= .env | cut -d= -f2-)
curl -s -X POST http://localhost:8000/v1/extract-price -H "content-type: application/json" -H "authorization: Bearer $TOKEN" -d '{"url":"https://www.pbtech.co.nz/product/MONLGL33262/LG-UltraGear-32G620B-B-32-QHD-200Hz-Gaming-Monitor"}'
```

## Risky files / rollback points

- **`routes/fetch.py` refactor (step 3)** — the shared-helper extraction must leave `/v1/fetch` behavior bit-identical. Rollback: revert the helper inline. Guarded by existing `tests/` (fetch route + signatures) which assert exact response contracts.
- **`pyproject.toml` httpx move (step 9)** — if anything already relied on httpx being dev-only, no impact (import works either way). Rollback: move back to dev extras.
- **`schemas.py` new reason** — closed-Literal change; only callers that switch on `reason` (none in this repo; iris switches on its own union) are affected. Documented in `api-spec.md`.
- **AI provider free-tier flakiness** — 429/503 are expected (iris already handles). `ARGUS_AI_MAX_RETRIES=3` + backoff covers it; terminal failure → `extraction_failed` (never throws to caller).

## Sub-agent context manifests (curate before `task.py start` — Pi is sub-agent-dispatch)

`implement.jsonl` and `check.jsonl` currently hold only the seed `_example`
row. Before `task.py start`, replace the seed with real spec/research entries
(quality bar: ≥1 real entry each). Planned entries:

- `implement.jsonl`:
  - `{file: ".trellis/spec/backend/directory-structure.md", reason: "flat single-package layout; one concern per file; routes one file per endpoint group — new jsonld.py/ai.py/routes/extract.py must follow"}`
  - `{file: ".trellis/spec/backend/error-handling.md", reason: "never-throw contract; map AI/fetch failures to {ok:false,...} JSON; broad catches need # noqa: BLE001"}`
  - `{file: ".trellis/spec/backend/quality-guidelines.md", reason: "pydantic v2 idioms; tests pure-module offline with SimpleNamespace app.state; assert exact JSON bodies"}`
  - `{file: "/Users/howard/Sources/iris/packages/prices/src/pipeline/ai-extract.ts", reason: "reference impl to port: reducePageHtml, single generateText, throttle, 429/503 retry, never-throws"}`
  - `{file: "src/argus/routes/fetch.py", reason: "shared navigation path to factor into helper; thin-route pattern to mirror in routes/extract.py"}`
- `check.jsonl`:
  - `{file: ".trellis/spec/backend/quality-guidelines.md", reason: "verification commands + forbidden patterns table to check against"}`
  - `{file: ".trellis/spec/backend/logging-guidelines.md", reason: "secrets rules — AI key never logged; key=value logging"}`
  - `{file: "docs/api-spec.md", reason: "contract the new endpoint + extraction_failed reason must be documented here"}`
