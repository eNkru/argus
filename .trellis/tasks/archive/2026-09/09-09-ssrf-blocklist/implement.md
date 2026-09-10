# Implement — SSRF fetch-target blocklist

> Ordered execution plan. Run `pytest` at every gate. No git commit until the
> Phase 3 review gate.

## Pre-flight (spec context)

- Load `.trellis/spec/backend/quality-guidelines.md` (Forbidden Patterns,
  Testing Requirements) and `error-handling.md` before writing code.
- Confirm the venv + suite is green before starting:
  `source .venv/bin/activate && pytest -q` (expect 37 passing).

## Step 1 — pure module + unit tests (no routes yet)

1. Create `src/argus/urlguard.py` with the surface in `design.md`: imports,
  `_PRIVATE_V4_NETS`, `_PRIVATE_V6_NETS`, `_METADATA_HOSTS`,
  `is_private_target(url) -> bool`, `guard_url(url, settings, tracker)`.
2. `guard_url` imports `HtmlFailed` from `.navigate` — but avoid a circular
   import: `navigate.py` will import `urlguard`. Resolve by defining `HtmlFailed`
   in `navigate.py` (it already is) and having `urlguard` import it lazily
   inside `guard_url` (`from .navigate import HtmlFailed`), OR define the return
   as a sentinel and let the caller build `HtmlFailed`. **Decision:** return a
   bool `is_blocked` from `guard_url` and let each caller construct `HtmlFailed`
   / `FetchImageResponseFail` — keeps `urlguard` dependency-free (no import of
   `navigate`/`routes`). Revise the design's return type accordingly; the
   behavior contract (R4) is unchanged.
3. Add `Settings.fetch_allow_private: bool = False` to `config.py`.
4. Add `tests/test_urlguard.py` (table-driven positives/negatives + the
   allow-private escape hatch).
5. **Gate:** `pytest -q tests/test_urlguard.py` green.

## Step 2 — wire `navigate.fetch_html`

1. At the top of `fetch_html` (before `ensure_browser`), call
   `guard_url(request.url, browser_manager._settings, tracker)`. If blocked,
   `return HtmlFailed()`. (Caller `_do_fetch` / `_do_extract` already map
   `HtmlFailed` → `fetch_failed`.)
2. **Gate:** `pytest -q` — existing fetch/extract tests green; add one
   route-level test asserting a private target returns `fetch_failed` and never
   calls `ensure_browser`.

## Step 3 — wire `routes/fetch_image._do_fetch_image`

1. Same guard at the top of `_do_fetch_image`, before `ensure_browser`; on
   block, `return FetchImageResponseFail(reason="fetch_failed")`.
2. **Gate:** `pytest -q` — full suite green; add the fetch-image private-target
   test.

## Step 4 — docs + env example

1. Add `ARGUS_FETCH_ALLOW_PRIVATE` to `.env.example` with a comment explaining
   the default-block + escape hatch.
2. Add a one-line invariant to `.trellis/spec/backend/error-handling.md` or a
   new `security-guidelines.md`: "Fetched URLs are blocked from private /
   loopback / link-local / metadata targets by default; `ARGUS_FETCH_ALLOW_PRIVATE`
   opts in for internal-scrape deployments."

## Validation (Phase 2.2 / 3.1 — last iteration, full scope)

- `pytest -q` — all green, count grew by the new tests.
- `grep -nIE "169\.254\.169\.254|is_private_target|blocked_url" src/ tests/`
  — confirms wiring and no stray references.
- Manual (optional, needs browser): with `ARGUS_AUTH_DISABLED=true` for a local
  test, `curl -s localhost:8000/v1/fetch -d '{"url":"http://169.254.169.254/latest/meta-data/"}'`
  → `{ok:false,reason:"fetch_failed"}`; same with `ARGUS_FETCH_ALLOW_PRIVATE=true`
  → reaches the network (block lifted). Skip if no browser binary — the unit +
  route tests are the gate.

## Review gate (before commit)

- R1 (byte-identical legit bodies) — existing tests unchanged. ✓
- R2 (secure default) — `fetch_allow_private` defaults False. ✓
- R4 (no new reason enum) — reused `fetch_failed`. ✓
- R5 (escape hatch) — `True` bypasses. ✓
- R6 (pure-module tests) — `test_urlguard.py` offline. ✓

## Rollback

Single feature: revert the `guard_url` calls in `navigate.fetch_html` and
`routes/fetch_image._do_fetch_image`, and `Settings.fetch_allow_private`. The
new `urlguard.py` + its tests can stay (dead code, harmless) or be deleted.
