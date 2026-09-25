# Journal - Howard Ju (Part 1)

> AI development session journal
> Started: 2026-08-20

---

## 2026-08-21 — dev.sh one-command local launcher (no task, inline)

**What**: Added `dev.sh` — idempotent script that boots the argus dev server
from a clean checkout (venv + deps → Camoufox binary → dev bearer token in
`.env` → `exec uvicorn --reload`). Subsequent runs skip satisfied steps.

**Why**: README had a 4-step manual Run locally flow; the real pain point was
the bearer token living only in the shell that started the server, so every
curl from another shell needed the same token re-exported. Persisting it to
`.env` (gitignored) fixes that.

**Files**: `dev.sh` (new), `README.md` (Run locally section rewritten:
`./dev.sh` is now primary; added Swagger/ReDoc/openapi.json endpoints;
manual steps kept as reference). Verified live: `/health` ok, `/docs` 200,
authed `/v1/fetch` returns real HTML, 37/37 pytest pass.

**Commit**: `ea4e5ce`. No Trellis task was created for this (inline work).



## Session 1: Price extraction end-to-end — JSON-LD first, AI fallback

**Date**: 2026-08-23
**Task**: Price extraction end-to-end — JSON-LD first, AI fallback
**Branch**: `main`

### Summary

Added POST /v1/extract-price (JSON-LD-first, AI fallback) to argus. Two phases: Phase A deterministic extractor + shared navigate.py refactor (ad7a918); Phase B OpenAI-compatible LLM fallback ported from iris (75d4229). Reverses argus's documented 'AI extraction stays in caller' boundary — now documented in architecture.md. 85 tests pass (48 new), all offline, <3s.

### Main Changes

# Price extraction end-to-end — JSON-LD first, AI fallback

## Goal & outcome

Added `POST /v1/extract-price` to argus — a new bearer-gated endpoint that
fetches a page through the shared browser pipeline and returns a parsed
product price (+ rich Product node) in a single call. Two stages:

1. **Deterministic JSON-LD parse** (no LLM) — schema.org Offer/AggregateOffer
   with a sanity-guarded positive price. Returns the full primary Product
   node as `jsonld` (name/brand/gtin/reviews ride along).
2. **AI fallback** — on a JSON-LD miss with `aiFallback:true`, an
   OpenAI-compatible LLM reads a reduced page and returns the flat verdict.
   Blocked WAF pages short-circuit before any LLM call (mirrors iris's
   `checkPrice`).

`/v1/fetch` stays pure transport; argus is now an **extraction sibling** to
its existing transport role, not a replacement for it.

## Boundary reversal (the architectural decision)

`docs/architecture.md` previously said "AI extraction — out of scope / stays
in the caller." This task reverses that: argus gains an LLM HTTP client, an
API-key secret (its first non-token secret), prompt design, and token
cost/latency on the fetch path. The user confirmed intent before
implementation. `architecture.md` now documents the new boundary:
- **Into argus**: price extraction (JSON-LD + AI), `/v1/extract-price`.
- **Stays in caller**: cross-product retry/backoff/pLimit, price history
  storage, iris-side wiring (follow-up).

## What was built

**Phase A (deterministic path, shipped alone at `ad7a918`):**
- `src/argus/jsonld.py` — pure recursive walker for `<script type="application/ld+json">`,
  tolerates concatenated objects / `@graph` / list `@type` / AggregateOffer.
  Decimal-normalized 2dp prices, >0 guard, Product-ancestor tracking.
- `src/argus/navigate.py` — navigation pipeline extracted from `routes/fetch.py`
  so the cookie-jar `finally` cleanup lives once across both routes.
- `src/argus/routes/fetch.py` refactored onto the helper, behavior identical
  (all 37 pre-existing tests still pass; diffed line-by-line by check agent).
- `src/argus/routes/extract.py` — thin route: fetch → blocked short-circuit →
  JSON-LD hit, or `extraction_failed` on miss.
- `src/argus/schemas.py` — `ExtractPriceRequest` (+ `aiFallback:true`),
  Ok/Fail models with the new `extraction_failed` reason (closed-Literal
  contract change → documented in `api-spec.md`).
- `docs/api-spec.md` + `README.md` endpoint table.
- `tests/test_jsonld.py` (16 tests, offline).
- `tests/test_extract_route.py` (12 tests, fake browser chain).

**Phase B (AI fallback, shipped at `75d4229`):**
- `src/argus/ai.py` — faithful port of iris `ai-extract.ts` (2026-08-23):
  raw-`httpx` chat-completions (no `openai` SDK), `reduce_html` with 8k/3k/3
  caps, single-call prompt (avoids DeepSeek thinking-mode `reasoning_content`
  drop, iris §1e), boot-time `asyncio.Semaphore` + live min-interval gap,
  429/502/503/504 retry with exponential backoff + jitter, never-throws.
  `httpx.MockTransport` injectable for offline tests.
- `src/argus/config.py` — 9 `ARGUS_AI_*` fields, `ai_api_key` marked secret.
- `src/argus/main.py` — `AiClient` singleton on `app.state` (None when
  unconfigured), `aclose` on shutdown.
- `pyproject.toml` — `httpx` promoted dev→runtime.
- `docs/architecture.md` — documented the boundary reversal.
- `README.md` — `ARGUS_AI_*` config table rows.
- `.env.example` — full AI block.
- `tests/test_ai.py` (18 tests, mocked httpx).
- 2 new route tests for the `source:"ai"` arm.

## Live verification (ac-against-the-real-thing)

- **pbtech PDP** → `source:"jsonld"`, `price:"499.00"`, `currency:"NZD"`,
  full Product node incl. `gtin-13`, `brand`, `aggregateRating`, `sku`, `mpn`.
- **kogan PDP** → `source:"jsonld"`, `price:"264.00"`, `currency:"NZD"`,
  Product node incl. reviews + `gtin` + `category`.
- **farmers PDP** → WAF started rate-limiting this dev IP after ~40 session
  fetches (proven Akamai rate-based escalation); both block signatures
  (`akamai-behavioral-challenge`, `akamai-empty-shell`) classify correctly
  with `retryable:true` surfaced. The farmers pass path was proven via the
  extractor against the saved live HTML (`599.99 NZD`); the route path is
  identical to pbtech/kogan which are proven live.
- **/v1/fetch post-refactor** → regression-checked live, behavior identical.
- **AC4 (unconfigured AI degradation)** → `extract-price` on example.com
  returns `extraction_failed` with no 500. argus behaves as a deterministic
  JSON-LD-only service until the operator sets `ARGUS_AI_*`.

## Process notes worth recording

- **Sub-agent infra flaked twice today.** First `trellis-implement` dispatch
  died on a provider error after only reading files (`stopReason:"error"`).
  Second died on a clean `stop` mid-docs. Every sub-agent output was
  diff-reviewed before acceptance; gaps (Phase A: full implementation;
  Phase B: `tests/test_ai.py` + `architecture.md` + README) were finished
  inline by the main session. Net result: 0 lines of unverified code shipped.
- **Check agent did a thorough job on Phase A**: found 3 minor violations
  (dead constant, misleading log gating, missing `nullable:true`), verified
  the `fetch.py` refactor equivalence line-by-line (tracker kinds, exception
  ladder, cleanup nesting all preserved).
- **The API surface decision (new endpoint vs. enrich `/v1/fetch`)** is
  the highest-leverage choice and was confirmed upfront with the user; it
  kept `/v1/fetch` pure transport (architecture.md still consistent with
  that role) and made the AI fallback naturally opt-in by construction.

## Files changed

```
src/argus/ai.py              (new, 15.8KB)
src/argus/jsonld.py          (new, ~9KB)
src/argus/navigate.py        (new, shared navigation pipeline)
src/argus/routes/extract.py  (new, thin route)
src/argus/routes/fetch.py    (refactored onto navigate.fetch_html)
src/argus/schemas.py         (+ ExtractPriceRequest/ResponseOk/Fail)
src/argus/config.py          (+ 9 ARGUS_AI_* fields)
src/argus/main.py            (AiClient on app.state, aclose on shutdown)
pyproject.toml               (httpx dev→runtime)
.env.example                 (+ AI block, all 9 vars documented)
docs/api-spec.md             (+ /v1/extract-price path)
docs/architecture.md         (boundary-reversal section)
README.md                    (endpoint table + ARGUS_AI_* config table)
tests/test_jsonld.py         (16 new tests)
tests/test_extract_route.py  (12 new tests; +2 source=ai tests in Phase B)
tests/test_ai.py             (18 new tests, mocked httpx)
.trellis/spec/backend/directory-structure.md
                             (module layout + ai.py throttle exception)
```

## Test coverage (final)

- **85 passed** (37 pre-existing + 48 new for this task), full suite < 3s
- All tests offline: pure modules, `SimpleNamespace` fakes, `httpx.MockTransport`,
  monkeypatched `_sleep` indirection. Zero camoufox import, zero network.
- One acceptance item not unit-tested: live network smoke against a real
  LLM provider. Requires `ARGUS_AI_*` credentials (not in the dev `.env`);
  the operator runs the smoke once their key is in place.


### Git Commits

| Hash | Message |
|------|---------|
| `ad7a918` | (see git log) |
| `75d4229` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: Security hardening: 3/7 findings fixed (SSRF, docs, dev-token)

**Date**: 2026-09-10
**Task**: Security hardening: 3/7 findings fixed (SSRF, docs, dev-token)
**Branch**: `security-hardening`

### Summary

Read-only audit found 7 security findings; planned a 1-parent + 7-child Trellis tree on branch security-hardening. Implemented + archived 3 fully-verifiable, default-secure children without breaking any existing behavior (pytest: 85 pre-existing unchanged, 119 total green): #1 SSRF fetch-target blocklist (new urlguard.py wired into all 3 fetch routes before begin_fetch; blocks 169.254.169.254/loopback/RFC1918/metadata hostnames; ARGUS_FETCH_ALLOW_PRIVATE escape hatch, default off; maps to existing fetch_failed contract = zero contract change), #3 docs gated behind ARGUS_DOCS_ENABLED (default off; dev.sh keeps /docs locally; main.py gains a module-scope _settings reused later by rate-limiting), #7 dev.sh no longer echoes the bearer token. Remaining 4 children (ai-trust-boundary, inbound-rate-limiting, dependency-lockfile-audit, container-hardening) left in planning as follow-up; #5/#2 need a working docker to verify the image build/non-root user and must not be done blind per the 'don't break anything' bottom line.

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `01fd9e1` | (see git log) |
| `b4edb0c` | (see git log) |
| `cb6564e` | (see git log) |
| `c49a910` | (see git log) |
| `745f2b0` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: Dependency lockfile + pip-audit CI gate (Finding #5)

**Date**: 2026-09-25
**Task**: Dependency lockfile + pip-audit CI gate (Finding #5)
**Branch**: `main`

### Summary

Completed security-hardening Finding #5: generated requirements.lock (pip-compile --generate-hashes) from the audit-verified installed set with zero version bumps (camoufox==0.5.4 intact), built the Docker image from the lockfile, and added scripts/audit.sh (pip-audit --disable-pip) as a must-pass supply-chain gate. pytest green, pip-audit clean, docker build healthy. Merged via PR #2.

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `3c3ebc0` | (see git log) |
| `1152853` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: Container hardening — non-root user, drop wget (Finding #2)

**Date**: 2026-09-25
**Task**: Container hardening — non-root user, drop wget (Finding #2)
**Branch**: `main`

### Summary

Completed security-hardening Finding #2: image now runs uvicorn+Camoufox as non-root argus (uid/gid 1000, USER argus before CMD); HOME=/home/argus so the camoufox browser cache is user-owned (font-prune paths moved to match); venv + cache chowned to argus; wget removed from the image and the compose healthcheck switched to a venv-python urllib probe. Live-verified: build ok, healthcheck healthy, exec id uid=1000(argus), which wget not found, browser binary readable, font prune intact; pytest 119 green, pip-audit clean, zero Python change. Also fixed 2 stale wget references in docs/deploy-qnap.md and captured the container invariants in backend/security-guidelines.md.

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `f71884e` | (see git log) |
| `ce58346` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete
