# Dependency lockfile + pip-audit CI gate

> Parent: `09-09-security-hardening`. Finding #5 (Low). Lightweight — build tooling.

## Goal

Pin the full transitive dependency set to a committed **lockfile** (no version
bumps), build the Docker image from the lockfile instead of duplicated `>=`
pins, and add a `pip-audit` check that must pass. Restores reproducibility and
gates the supply-chain (a malicious upstream release is no longer silently
pulled at the next build).

Today `pyproject.toml` pins only `camoufox==0.5.4`; everything else
(`fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `httpx`) floats on `>=`.
The `Dockerfile` **re-duplicates** that list in a `RUN pip install ...` (with a
"keep in sync with pyproject.toml" comment) — a drift risk. `pip-audit` on the
currently-installed set is clean (verified during the audit), so this is
hardening, not an active CVE fix.

## Requirements

- **R1** Generate a lockfile from the **currently-installed** versions (the
  versions the audit verified clean) — DO NOT bump anything. `camoufox==0.5.4`
  stays exactly pinned (browser-build contract).
- **R2** Build the Docker image from the lockfile (replace the duplicated
  `>=` list in the `RUN pip install` with an install from the lockfile), so
  the image and local env resolve identically.
- **R3** Add a `pip-audit` invocation (a script or a documented CI command)
  that scans the locked set and must pass (exit 0). Wire it where CI would run
  it; if there's no CI yet, add a `make audit` / `./scripts/audit.sh` one-liner
  and note it in the README or `quality-guidelines.md` verification commands.
- **R4** No runtime behavior change — same packages, same versions, same
  `pyproject.toml` `[project].dependencies` (the lockfile is additive). The
  dev extras (`pytest`, `pytest-asyncio`) may be locked too or left floating;
  prefer locking for consistency.

## Acceptance criteria

- [ ] A committed lockfile (e.g. `requirements.lock` from `pip-compile`, or
  `uv.lock` from `uv lock`) whose resolved versions match the audit-verified
  `pip list` output (fastapi 0.141.1, uvicorn 0.52.4, pydantic 2.13.4,
  pydantic-settings 2.15.0, httpx 0.28.1, starlette 1.6.0, camoufox 0.5.4, …).
- [ ] `Dockerfile` installs from the lockfile; `docker compose build` succeeds
  and the healthcheck passes.
- [ ] `pip-audit` against the lockfile → "No known vulnerabilities found".
- [ ] `pytest -q` green (no code change — R4).
- [ ] `pyproject.toml [project].dependencies` unchanged (lockfile is additive).

## Design decision — pip-compile vs uv.lock

- **pip-compile (pip-tools):** produces `requirements.lock` (a flat pinned
  list with hashes optional). The existing build is pip-based (`pip install`),
  so `pip install -r requirements.lock` is the lowest-friction change. Adds
  `pip-tools` as a dev extra. **Recommended** — minimal blast radius, matches
  the existing build.
- **uv.lock:** `uv lock` produces a cross-platform lockfile, but adopting uv
  for builds is a larger tooling shift (new `uv` invocation in the Dockerfile,
  `hatchling` build backend interaction). Defer unless the team is already on
  uv.

Choose pip-compile unless the operator objects. Either way, R1 (no version
bumps) is the hard constraint.

## Implementation notes

- Generate: `pip-compile --generate-hashes --output-file=requirements.lock
  pyproject.toml` (review the output for version bumps; if pip-compile wants to
  bump, pin each to the currently-installed version via constraints, e.g.
  `pip-compile` against a `constraints.txt` of `pip freeze` output).
- Dockerfile: replace the `RUN /opt/argus/bin/pip install --no-cache-dir
  "camoufox==0.5.4" "fastapi>=0.115" ...` line with
  `COPY requirements.lock ./` + `RUN /opt/argus/bin/pip install --no-cache-dir
  -r requirements.lock` (keep `camoufox fetch` after). Coordinate with
  `container-hardening` (which also edits the Dockerfile).
- Audit command: `.venv/bin/pip-audit -r requirements.lock` → must exit 0.

## Coordination

- `Dockerfile` also touched by `container-hardening`. Apply this child
  **first** (lockfile install line), then `container-hardening` (USER/chown) —
  see that child's coordination note.
