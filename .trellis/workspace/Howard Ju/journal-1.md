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

