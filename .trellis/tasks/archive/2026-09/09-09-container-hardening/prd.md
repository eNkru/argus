# Container hardening — non-root user, remove wget, prune apt

> Parent: `09-09-security-hardening`. Finding #2 (Medium). Lightweight — Dockerfile-only.

## Goal

Run the uvicorn + Camoufox process tree inside the image as a **non-root
user**, and **remove `wget`** (currently left installed) from the final image,
while keeping the image buildable, the healthcheck passing, and the Camoufox
browser binary readable at runtime.

Today the `Dockerfile` has no `USER` directive, so the CMD
`uvicorn argus.main:app --host 0.0.0.0 --port 8000` runs as root. A browser or
Playwright compromise would land as root in the container. `wget` is installed
(used only by the `docker-compose.yml` healthcheck `wget -qO- .../health`).

## Requirements

- **R1** Add a non-root user/group (e.g. `argus:argus`) and `USER argus` before
  `CMD`.
- **R2** Make the Camoufox browser binary — fetched at build time into
  `/root/.cache/camoufox/browsers/...` — readable by the non-root user at
  runtime. Approach: relocate the fetch to a path owned by `argus` (e.g. set
  `ENV HOME=/home/argus` before the `RUN camoufox fetch` so the cache lands in
  `/home/argus/.cache/...`, then `chown -R argus:argus /home/argus`), and ensure
  the `apt`-installed GTK/NSS libs remain world-readable (they are by default).
- **R3** Remove `wget` from the apt install list **and** switch the
  `docker-compose.yml` healthcheck to not depend on it. The venv has Python +
  `urllib`; use `python -c "import urllib.request,sys;
  urllib.request.urlopen('http://localhost:8000/health'); sys.exit(0)"` (or
  `httpx` since it's a dep). Update the healthcheck `test` in `docker-compose.yml`
  accordingly. Healthcheck must still pass on `docker compose up`.
- **R4** Drop any other unneeded apt package from the install list if obvious
  (e.g. if `build-essential` is only needed at the `pip install` stage and not
  at runtime — but it's installed before the venv build, so it may be needed
  for compiling any wheel; verify before removing). Conservative: only remove
  `wget` if unsure. Do not remove GTK/NSS/X11 libs (Camoufox needs them).

## Acceptance criteria

- [ ] `Dockerfile` has `USER argus` (or equivalent) before `CMD`.
- [ ] `docker compose build` succeeds; `docker compose up` healthcheck reaches
  `healthy` (verify via `docker inspect` health status or `curl .../health`).
- [ ] `docker compose exec argus id` → `uid=1000(argus)` (non-root).
- [ ] `wget` absent from the final image (`docker compose exec argus which
  wget` → empty / not found), or if kept, a documented reason.
- [ ] Healthcheck uses Python (no `wget`), still passes.
- [ ] No source-code change outside `Dockerfile` / `docker-compose.yml` /
  `.env.example` (no Python behavior change — R1 of parent).

## Implementation notes (gotchas)

- **Camoufox cache path:** the existing `Dockerfile` prunes
  `/root/.cache/camoufox/browsers/.../fonts/{macos,windows}`. If `HOME` moves
  to `/home/argus`, the prune paths must move to `/home/argus/.cache/...`. Keep
  the prune logic (it trims ~900MB of dead-weight fonts for the linux
  fingerprint).
- **`pip install --no-deps .`** still works as non-root at build time because
  the build RUNs before `USER argus` run as root; only the runtime `CMD` runs
  as `argus`. So the venv at `/opt/argus` must be readable+executable by
  `argus` — `chown -R argus:argus /opt/argus` after install (still as root,
  before `USER`).
- **Healthcheck as non-root:** the compose healthcheck runs as the container
  user by default; `python` from `/opt/argus/bin` must be on PATH (it is —
  `ENV PATH="/opt/argus/bin:${PATH}"`).

## Coordination

- `Dockerfile` is also touched by `dependency-lockfile-audit` (the
  `pip install` line → lockfile). Apply **lockfile-audit first**, then this
  child, so the `USER`/`chown` land on the final install shape. No formal
  `blockedBy` (not a hard dep), just sequence in one session.
