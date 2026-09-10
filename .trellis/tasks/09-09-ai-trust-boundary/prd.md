# AI-extracted price trust boundary — spec + optional allow-list

> Parent: `09-09-security-hardening`. Finding #6 (Low). Lightweight — mostly spec.

## Goal

Document that **AI-extracted prices are untrusted** (a data-integrity /
prompt-injection surface, not a server-breach surface) in the backend spec, and
add an **optional, default-empty domain allow-list** for the AI stage so an
operator can restrict the LLM fallback to trusted retailer domains — without
changing any behavior when the allow-list is empty (the default).

Today `/v1/extract-price` with `aiFallback=true` sends a reduced version of an
**attacker-controlled page** (`ai.py::reduce_html` → visible text + price blobs)
to an LLM and trusts its `{price, currency, name, available}` output. A
malicious retailer page (or MITM) could craft text/blobs that instruct the
model to return an arbitrary price. The blast radius is bounded — the
`_ParsedExtraction` discriminated-union validator (`ai.py`) constrains output
to that shape with `price: float gt=0`, so the worst case is a *wrong* price
returned to the caller, not a server compromise. But any downstream logic that
treats the AI price as authoritative is at risk.

## Requirements

- **R1 — spec (the real fix):** add to `.trellis/spec/backend/` a
  `security-guidelines.md` (new) — or extend `quality-guidelines.md` Forbidden
  Patterns — stating:
  - AI-extracted prices (`source="ai"`) are **UNTRUSTED** and must never be the
    authoritative price in any financial / inventory / pricing decision; the
    JSON-LD path (`source="jsonld"`) is the trusted path; AI is a best-effort
    fallback only.
  - The schema-boundary invariant: `_ParsedExtraction` (discriminated on
    `available`, `price: float gt=0`, `currency` length-bounded) is the
    containment; do not relax it.
  - The prompt is fed attacker-controlled page content; never extend the LLM's
    output shape (no tools, no multi-step — `ai.py` already enforces this).
- **R2 — optional allow-list (default empty = no behavior change):**
  `Settings.ai_extract_domain_allowlist: str = ""` (`ARGUS_AI_EXTRACT_DOMAIN_ALLOWLIST`).
  Comma-separated retailer host suffixes, e.g. `"pbtech.co.nz,kogan.co.nz"`.
  When non-empty, the AI stage in `routes/extract.py` runs **only** if
  `urlparse(result.final_url).hostname` ends with one of the listed suffixes;
  otherwise it degrades to `extraction_failed` with an INFO log
  (`ai_stage_skipped url=%s reason=host_not_allowlisted`) — no LLM call, no
  cost. When empty (default), behavior is byte-identical to today.
- **R3 — regression test:** a test asserting the allow-list-empty case still
  calls the AI client (current behavior), and a non-empty allow-list with a
  non-matching host skips the AI call (returns `extraction_failed` without
  invoking the client). Use the fake `app.state.ai_client` pattern
  (`tests/test_extract_route.py`).

## Acceptance criteria

- [ ] `.trellis/spec/backend/security-guidelines.md` (or equivalent) documents
  the AI-price trust boundary + the schema-containment invariant (R1).
- [ ] `Settings.ai_extract_domain_allowlist` defaults `""` (R2); documented in
  `.env.example`.
- [ ] Empty allow-list → AI stage runs exactly as today (existing
  `test_extract_route.py` AI tests unchanged; R3 confirms).
- [ ] Non-empty allow-list + non-matching host → no AI client call, returns
  `extraction_failed`, INFO log (R3).
- [ ] Full suite green; no Python behavior change with default config (R1 of
  parent: byte-identical for the empty-allow-list case, which is the default).

## Constraints

- Inherits parent R1–R6. The default-empty allow-list satisfies R2 (opt-in)
  and R1 (byte-identical default behavior). No new dep. No browser in tests.

## Notes

- This is the lowest-severity finding; the **spec documentation (R1)** is the
  substance — it prevents a future contributor from promoting AI prices to
  authoritative use or from loosening the schema validator. The allow-list
  (R2) is a convenience for operators who want to scope the LLM cost/risk.
- Do NOT add per-site branching in the fetch path (forbidden pattern in
  `quality-guidelines.md`) — the allow-list gates the **AI stage only**, not
  the fetch, and is a single hostname-suffix match, not per-retailer code.
