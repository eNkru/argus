# Logging Guidelines

> Structured logging conventions in this project.

---

## Overview

Stdlib `logging` only. Root logging configured once in `main.py`; every module uses a **dot-namespaced logger** with lazy `%s` formatting.

```python
# main.py — the only basicConfig call
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# per module
logger = logging.getLogger("argus.fetch")   # argus.browser, argus.render, argus.diagnostics, ...
```

---

## Format

Messages are **key=value structured fields appended to a short prose prefix**, so logs are greppable without a log-aggregation schema:

```python
logger.warning(
    "fetch non-2xx status (returning HTML for classification) url=%s status=%d final_url=%s html_len=%d",
    request.url, response.status, final_url, len(html),
)
```

Established field keys: `url=`, `status=`, `error_type=`, `error=`, `count=`, `attempt=`, `consecutive_failures=`, `final_url=`, `html_len=`.

`error_type` is the qualified exception class name (`module.QualName`) — use the `diagnostics._exc_type_name` helper pattern rather than bare `type(exc)`.

---

## Log Levels

| Level | Use | Examples |
|---|---|---|
| `info` | Lifecycle events, normal-but-noteworthy retries | browser launch/teardown, lazy-launch ready, content mid-navigation retry |
| `warning` | Per-request failures, cleanup errors, degradation counts | fetch timeout, cookie injection failed, page close failed, browser degraded |
| `debug` | High-volume / secret-adjacent detail | cookie injection count |

The "browser degraded" threshold diagnostic (`diagnostics.py`) fires `warning` **exactly once** when consecutive failures cross `DIAGNOSE_THRESHOLD = 3`, with a full traceback — not on every failure.

---

## Rules

1. **Always lazy `%s`-style formatting** — never f-strings in log calls.
2. **Cookies (and tokens) are secrets: never log names, values, or the jar.** Log `count=%d` only. This is enforced convention, not optional.
3. **URLs may be logged** (they are request inputs, not secrets).
4. `html_len=` instead of logging HTML bodies.
5. No third-party logging libs, no JSON-formatted logging — keep the plain key=value line format.

---

## Anti-patterns

- `logger.warning(f"...")` — eager formatting.
- Logging `exc` at `error` level for expected per-request failures — timeouts are `warning`; `error` is reserved for nothing in the current codebase (degradation is surfaced via the threshold warning).
- Per-request `info` spam on the happy path — the happy path is silent; logs mark deviations.
