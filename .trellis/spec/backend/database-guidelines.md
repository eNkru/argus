# Database Guidelines

> Persistence patterns in this project.

---

## Overview

**Argus has no database.** It is a stateless HTTP fetch transport: request in → browser fetch → response out. All state is either per-request (ephemeral `BrowserContext`) or process-local (`BrowserManager`, `FailureTracker` singletons on `app.state`).

---

## Rules

1. **Do not introduce a database, ORM, or persistent storage** without an explicit design conversation — it would change the deployment story (the Docker image is stateless by design).
2. **No state may leak between requests.** Every fetch runs in a fresh `BrowserContext` created in `routes/fetch.py` and destroyed in a `finally` — cookie jars never persist across callers. New endpoints must follow the same fresh-context pattern.
3. **In-process state belongs on `app.state`**, constructed in `main.lifespan`. Counter-like diagnostics (`FailureTracker`) are process-local and intentionally reset on restart; that is acceptable for their purpose.
4. If a future feature genuinely needs persistence (e.g. persistent login sessions — see `docs/future.md`), treat it as a design task first: the deferred-work list in `docs/future.md` is the parking lot.

---

## Anti-patterns

- Module-global mutable state (was removed when porting from the iris sidecar; see `browser.py` docstring).
- Caching fetched HTML / cookies to disk — cookies are secrets and must never be written anywhere (see logging-guidelines).
