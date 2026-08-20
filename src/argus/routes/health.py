"""``GET /health`` — unauthenticated readiness probe.

Under the lazy lifecycle the browser is absent at boot by design, so readiness
cannot gate on the browser being resident. The *service* is ready as soon as the
lifespan has set up the semaphore / lock / idle watcher. Whether the browser is
currently resident is reported as an informational field.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health", response_model=None)
async def health(request: Request) -> dict[str, str]:
    browser_manager = request.app.state.browser_manager
    return {
        "status": "ok",
        "browser": "ready" if browser_manager.is_ready else "absent",
    }
