# Error Handling

> How errors are handled in this project.

---

## Overview

Argus's core error-handling contract: **the service never throws to the caller on the fetch path.** Browser/navigation failures map to structured JSON failure responses. Errors are logged with diagnostic detail and counted by the `FailureTracker`, but the HTTP response is always a well-formed `{ok: false, ...}` body.

---

## Error Types

There are **no custom exception classes**. Failures are represented as Pydantic response models in `src/argus/schemas.py`:

```python
class FetchResponseFail(BaseModel):
    ok: bool = False
    reason: Literal["blocked", "fetch_failed"]
    signature: Optional[str] = None   # present only when reason="blocked"
    retryable: Optional[bool] = None  # argus owns the signature registry
```

Failure reasons are a closed `Literal` set — adding a new reason is an API contract change and must update `docs/api-spec.md`.

---

## Error Handling Patterns

### 1. Catch-all at the route boundary (`routes/fetch.py`)

```python
try:
    browser = await browser_manager.ensure_browser()
    ...
except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
    tracker.record_failure(request.url, exc, kind="timeout")
    return FetchResponseFail(reason="fetch_failed")
except Exception as exc:  # noqa: BLE001 — never throw to the caller
    tracker.record_failure(request.url, exc, kind="error")
    return FetchResponseFail(reason="fetch_failed")
```

Rules:
- Specific exception types first (`PlaywrightTimeoutError`, `asyncio.TimeoutError`), broad `Exception` last as the safety net.
- Every failure goes through `tracker.record_failure(url, exc, kind=...)` with a short `kind` label (`"timeout"` | `"error"` | `"no_response"` | `"cookie_injection"`) so failures without an exception object are still accounted.
- The deliberate `# noqa: BLE001` comments are the established idiom — broad catches at trust boundaries are intentional; keep the noqa with a reason.

### 2. Cleanup must not mask the result

Page/context closes happen in `finally` blocks with their own swallowed exceptions:

```python
finally:
    try:
        await page.close()
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the fetch result
        logger.warning("page close failed url=%s error_type=%s error=%s", ...)
```

### 3. Best-effort helpers never raise

`render.wait_for_render` and `snapshot_content` are contractually non-raising on best-effort paths (failures return/escalate rather than crash). If you add a page helper, keep this contract: only the caller's timeout/exception paths produce `fetch_failed`.

### 4. Blocked ≠ failed

A WAF challenge page is a *response*, not an error: the HTML is always snapshotted (including non-2xx), classified by `signatures.detect`, and returned as `{ok:false, reason:"blocked", signature, retryable}`. A detected block counts as `tracker.record_success()` for the degradation trend (per-site signal, not browser degradation).

### 5. Auth errors are the exception

`auth.require_token` raises plain FastAPI `HTTPException(401, ..., headers={"WWW-Authenticate": "Bearer"})` — auth failures *do* surface as HTTP errors, never as `{ok:false}` bodies.

---

## API Error Responses

| Situation | Response |
|---|---|
| Auth missing/invalid | HTTP 401 (WWW-Authenticate: Bearer) |
| Navigation/timeout/injection failure | HTTP 200, `{ok:false, reason:"fetch_failed"}` |
| WAF block detected | HTTP 200, `{ok:false, reason:"blocked", signature:"...", retryable:bool}` |
| Validation error (bad URL etc.) | FastAPI default 422 (pydantic `field_validator`) |

---

## Common Mistakes

- Re-raising inside `finally` cleanup — would mask the real fetch result (see pattern 2).
- Logging cookie names/values when cookie injection fails — log the count/type only (secrets).
- Treating non-2xx HTTP status as `fetch_failed` — the HTML is returned so classification can run; log a warning and continue.
