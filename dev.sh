#!/usr/bin/env bash
# Argus local dev launcher — the single command to start the service.
#
# Idempotently:
#   1. creates the virtualenv + installs editable deps (with dev extras) if missing
#   2. ensures the Camoufox browser binary is present (fetches on first run only)
#   3. ensures a dev bearer token exists in .env (generates one on first run;
#      .env is gitignored, so server and curl share the same token across runs)
#   4. starts uvicorn with --reload on :8000
#
# Usage:
#   ./dev.sh                 # start the dev server
#
# The server is ready when `curl http://localhost:8000/health` returns
# {"status":"ok",...}. The bearer token for /v1/* calls is printed on startup
# and lives in .env. To rotate it, delete the ARGUS_API_TOKENS line in .env
# and re-run.
#
# A token exported as ARGUS_API_TOKENS in your shell takes precedence over .env
# (pydantic-settings reads real env vars first).

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
CAMOUFOX="$VENV/bin/camoufox"
UVICORN="$VENV/bin/uvicorn"
PORT="${ARGUS_PORT:-8000}"

# --- 1. virtualenv + dependencies -------------------------------------------
if [ ! -x "$PY" ]; then
  echo "→ creating virtualenv ($VENV)"
  python3 -m venv "$VENV"
fi

if ! "$PY" -c "import argus, camoufox, fastapi, uvicorn" >/dev/null 2>&1; then
  echo "→ installing dependencies (editable, with dev extras)"
  "$PIP" install -q --upgrade pip >/dev/null
  "$PIP" install -q -e ".[dev]"
else
  echo "✓ dependencies already installed"
fi
# NOTE: if you add a new dep to pyproject.toml, re-run `pip install -e ".[dev]"`
# manually (or delete .venv to force a clean install on next ./dev.sh).

# --- 2. Camoufox browser binary (cached in ~/Library/Caches/camoufox) --------
if ! "$CAMOUFOX" active >/dev/null 2>&1; then
  echo "→ camoufox browser binary missing — fetching (one-time, ~200-900MB)"
  "$CAMOUFOX" fetch
else
  echo "✓ camoufox browser binary present ($("$CAMOUFOX" active))"
fi

# --- 3. dev bearer token in .env (gitignored) --------------------------------
ensure_dev_token() {
  local current=""
  if [ -f .env ]; then
    current=$(grep "^ARGUS_API_TOKENS=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' ')
  fi
  # Regenerate if missing, empty, or still the placeholder from .env.example.
  if [ -z "$current" ] || [ "$current" = "change-me" ]; then
    local new_token
    new_token=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
    if [ -f .env ] && grep -q "^ARGUS_API_TOKENS=" .env; then
      # token_urlsafe() yields only [A-Za-z0-9_-], so the sed replacement is safe.
      sed -i.bak "s|^ARGUS_API_TOKENS=.*|ARGUS_API_TOKENS=$new_token|" .env && rm -f .env.bak
    else
      printf 'ARGUS_API_TOKENS=%s\n' "$new_token" >> .env
    fi
    # Status messages go to stderr so they don't pollute the token value
    # returned to stdout (callers capture stdout for the startup banner).
    echo "→ generated dev bearer token and wrote it to .env" >&2
    current="$new_token"
  fi
  printf '%s' "$current"
}

if [ -n "${ARGUS_API_TOKENS:-}" ]; then
  TOKEN="$ARGUS_API_TOKENS"
  echo "✓ using ARGUS_API_TOKENS from environment"
else
  TOKEN="$(ensure_dev_token)"
fi

# --- 4. launch ---------------------------------------------------------------
echo
echo "────────────────────────────────────────────────────────────────────"
echo "  Argus dev server → http://localhost:$PORT  (reload on file change)"
echo "  Bearer token (for /v1/* curl): $TOKEN"
echo "  Docs:    http://localhost:$PORT/docs"
echo "  Health:  curl http://localhost:$PORT/health"
echo "────────────────────────────────────────────────────────────────────"
echo

# Local dev keeps the interactive API docs at /docs (the README references
# them). Production defaults ARGUS_DOCS_ENABLED to false (off) — see main.py.
export ARGUS_DOCS_ENABLED=true

# exec so Ctrl-C reaches uvicorn directly and signals propagate cleanly.
exec "$UVICORN" argus.main:app --reload --port "$PORT"
