# Security hardening — fix all audit findings without breaking function

## Goal

Remediate all 7 findings from the 2026-09-09 read-only security audit of the
Argus FastAPI + Camoufox fetch service, **without changing any existing
behavior for legitimate callers**. This parent task owns the cross-cutting
"don't break function" contract, the task map, and the final integration
review; the 7 children are independently plannable / implementable / checkable
/ archivable.

## Audit findings (source of truth)

| # | Severity | Finding | Child |
|---|----------|---------|-------|
| 1 | Medium-High | SSRF — no private/loopback/link-local/metadata filtering on fetched URLs | `ssrf-blocklist` |
| 2 | Medium | Docker image runs as root; `wget` left installed | `container-hardening` |
| 3 | Low-Medium | `/docs` `/redoc` `/openapi.json` exposed unauthenticated by default | `prod-docs-toggle` |
| 4 | Low-Medium | No inbound rate limiting (token brute-force / fetch abuse) | `inbound-rate-limiting` |
| 5 | Low | Dependencies float on `>=`; no committed lockfile; Dockerfile duplicates pins | `dependency-lockfile-audit` |
| 6 | Low | AI-extracted prices trusted from attacker-controlled pages (prompt-injection → wrong price) | `ai-trust-boundary` |
| 7 | Low (dev) | `dev.sh` prints the bearer token to the stdout banner | `dev-token-hygiene` |

## Task map

| Child | Weight | Artifacts | Touches |
|---|---|---|---|
| `ssrf-blocklist` | complex | prd + design + implement | `urlguard.py` (new), `config.py`, `navigate.py`, `routes/fetch_image.py` |
| `container-hardening` | lightweight | prd-only | `Dockerfile` |
| `prod-docs-toggle` | lightweight | prd-only | `main.py`, `config.py`, `dev.sh` |
| `inbound-rate-limiting` | complex | prd + design + implement | `ratelimit.py` (new), `config.py`, `main.py` |
| `dependency-lockfile-audit` | lightweight | prd-only | `pyproject.toml`, `Dockerfile`, lockfile (new) |
| `ai-trust-boundary` | lightweight | prd-only | `config.py`, `routes/extract.py`, `.trellis/spec/backend/*` |
| `dev-token-hygiene` | lightweight | prd-only | `dev.sh` |

No hard ordering dependencies between children (parent/child is not a
dependency system). Two **coordination notes** (same file touched by two
children, no blockedBy needed — just sequence within one session):

- `main.py` is touched by `prod-docs-toggle` (FastAPI kwargs) and
  `inbound-rate-limiting` (middleware). Apply both in one pass; they edit
  different regions (constructor kwargs vs `app.add_middleware`).
- `Dockerfile` is touched by `container-hardening` (USER + apt) and
  `dependency-lockfile-audit` (install line → lockfile). Apply lockfile-audit
  first so the non-root USER lands on the final install shape, then
  container-hardening.

## Cross-cutting requirements (the "don't break function" contract)

Every child MUST satisfy all of the following. These are the parent-level
acceptance criteria each child inherits:

- **R1 — Response contracts byte-identical for legitimate callers.** The exact
  JSON bodies of `/v1/fetch`, `/v1/fetch-image`, `/v1/extract-price`, `/health`,
  and the `401` auth path are unchanged for any input that succeeds today.
  `tests/` asserts full-body equality (`quality-guidelines.md`), so the
  existing 37-test suite staying green is the primary guard.
- **R2 — Secure defaults, opt-in for behavior change.** Any feature that could
  change behavior for an existing caller defaults to OFF (or to a value
  equivalent to today's behavior). Operators opt in via `ARGUS_*` env. Examples:
  `ARGUS_FETCH_ALLOW_PRIVATE` defaults `false` (block), `ARGUS_RATE_LIMIT_ENABLED`
  defaults `false` (off), `ARGUS_DOCS_ENABLED` defaults `false` (docs off in
  prod, dev.sh turns on), `ARGUS_AI_EXTRACT_DOMAIN_ALLOWLIST` defaults `""`
  (no restriction).
- **R3 — No new runtime dependency that shifts behavior** without review.
  `camoufox==0.5.4` stays pinned (browser-build contract). Rate limiting uses a
  self-contained in-memory limiter (no `slowapi`) to honor the lean-deps ethos
  (`ai.py` raw-httpx precedent). Lockfile child pins current versions, never
  bumps.
- **R4 — Secrets stay unlogged.** No token / cookie / API-key value reaches a
  log line. New code follows `logging-guidelines.md` (counts only, lazy `%s`).
- **R5 — Tests cover pure modules only; no browser/network in CI.** New logic
  that is pure (URL guard, rate limiter, allow-list match) lives in its own
  module with unit tests. Route-level behavior uses the existing fake-`app.state`
  + `Settings.model_construct(...)` pattern (`tests/conftest.py::make_settings`).
  Never spin up the real lifespan (imports camoufox).
- **R6 — Broad catches stay `# noqa: BLE001 — <reason>`** and never throw to the
  caller on the fetch path (`{ok:false, ...}` is the contract; 401 is the only
  exception).

## Parent acceptance criteria (final integration review)

- [ ] All 7 children archived.
- [ ] `pytest` green (37+ tests, count grows with new tests, all pass) with no
  browser binary or network.
- [ ] `pip-audit` clean on the locked dependency set.
- [ ] Docker image builds; `docker compose up` healthcheck passes; container
  runs as a non-root user; `/docs` returns 404 in prod defaults.
- [ ] One manual smoke pass: `curl` `/health` (200), `/v1/fetch` with a public
  URL (unchanged body), `/v1/extract-price` JSON-LD path (unchanged), and an
  SSRF probe (`http://169.254.169.254/`) returns `{ok:false,reason:"fetch_failed"}`
  with `ARGUS_FETCH_ALLOW_PRIVATE` unset.
- [ ] No secret values appear in any new log line or in the diff.
- [ ] `.trellis/spec/backend/` updated where a child captured a convention
  (AI-trust boundary; SSRF blocklist invariant).

## Notes

- This parent is NOT the implementation target. Do not `task.py start` it.
- Each child is started/archived independently; the parent is archived last
  after the integration review above passes.
- The audit itself was read-only and produced no code; findings live above.

## Follow-up (next session) — 2026-09-09 checkpoint

Session 2026-09-09 implemented and archived **3 of 7** children on branch
`security-hardening` (PR base `main`). All default-secure, all verified with
`pytest` (119 green: 85 pre-existing unchanged + 34 new), nothing broke:

| # | Child | Commit | Status |
|---|---|---|---|
| 1 | `ssrf-blocklist` | `b4edb0c` | archived |
| 3 | `prod-docs-toggle` | `cb6564e` | archived |
| 7 | `dev-token-hygiene` | `c49a910` | archived |

Remaining **4 children** stay in `planning` as follow-up. Recommended resume
order + verification notes:

| # | Child | Risk | Verification |
|---|---|---|---|
| 6 | `ai-trust-boundary` | low | `pytest`-verifiable; spec doc + optional `ARGUS_AI_EXTRACT_DOMAIN_ALLOWLIST` (default `""` = no change). **Start here.** |
| 4 | `inbound-rate-limiting` | medium | complex — new `ratelimit.py` + `main.py` middleware, default OFF; `pytest`-verifiable. Reuses the module-scope `_settings` that `prod-docs-toggle` introduced. |
| 5 | `dependency-lockfile-audit` | medium | `pip-compile` + `pip-audit` runnable offline; **the Dockerfile install-line change needs `docker compose build` to verify** — do not ship blind. |
| 2 | `container-hardening` | medium | Dockerfile non-root user + drop `wget` + healthcheck switch; **needs `docker compose build && docker compose up` + `docker compose exec argus id` to verify — cannot be proven without Docker.** Defer until Docker is available. |

To resume: `python3 ./.trellis/scripts/task.py start 09-09-ai-trust-boundary`
(or whichever child), branch already `security-hardening`. Do NOT implement #5
or #2 without a working `docker` to run the build/up verification gate — the
parent's bottom line is "don't break anything," and a blind Dockerfile change
violates it.
