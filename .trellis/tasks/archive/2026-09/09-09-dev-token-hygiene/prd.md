# dev.sh — stop printing bearer token to stdout banner

> Parent: `09-09-security-hardening`. Finding #7 (Low, dev-only). Lightweight.

## Goal

Stop echoing the bearer token in the `dev.sh` startup banner so it does not
land in terminal scrollback, shared-screen captures, or CI logs. The token
still gets generated and written to `.env` (that's required); only the stdout
echo changes.

Today `dev.sh` prints:
```
  Bearer token (for /v1/* curl): $TOKEN
```
in the launch banner. Convenient, but the token persists in the terminal.

## Requirements

- **R1** Replace the `Bearer token (for /v1/* curl): $TOKEN` banner line with
  a hint that does not reveal the value, e.g.:
  ```
  Bearer token: in .env (ARGUS_API_TOKENS) — view with: grep ARGUS_API_TOKENS .env
  ```
  Keep the rest of the banner (`http://localhost:$PORT`, Docs, Health lines).
- **R2** The token-generation + `.env` write logic (`ensure_dev_token`) is
  unchanged — only the echo is removed. A shell env `ARGUS_API_TOKENS` still
  takes precedence and still works.
- **R3** No runtime / server behavior change (this is a dev launcher script).
  The README's documented `curl -H "Authorization: Bearer ..."` usage still
  works (the user reads the token from `.env`).

## Acceptance criteria

- [ ] `./dev.sh` banner no longer contains the token value.
- [ ] Token still generated on first run and written to `.env` (verify
  `grep ARGUS_API_TOKENS .env` returns the value after a fresh `dev.sh` on a
  token-less `.env`).
- [ ] `dev.sh` still starts uvicorn on :8000 (manual smoke — no automated test
  for the launcher; `pytest` unaffected since `dev.sh` isn't imported).
- [ ] No Python source change (R1 of parent: zero runtime behavior change).

## Notes

- Dev-only. Lowest severity. Pairs naturally with `prod-docs-toggle` (which
  edits `dev.sh` to set `ARGUS_DOCS_ENABLED=true`) — land both `dev.sh` edits
  in one pass if convenient.
