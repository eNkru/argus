"""Inbound request rate limiting — fixed-window, per-client-IP, in-memory.

A self-contained limiter (no ``slowapi`` / external store) that wraps the whole
app as ASGI middleware, so it runs **before** ``auth.require_token``: the 401
bearer-token brute-force path is throttled exactly like any other ``/v1/*``
request. Default OFF (``Settings.rate_limit_enabled=False``) so existing
behavior is byte-identical until an operator opts in — mirrors ``ai.py``'s
lean-deps / throttle style (plain ``dict`` + ``time.monotonic``, no shared
store).

**Single-process limitation (documented, not fixed here):** state lives in one
instance's dict. Under multiple uvicorn workers or a load balancer each worker
gets its own budget — a shared store (Redis etc.) would be a new runtime
dependency. Argus runs one uvicorn process today, so a per-process fixed window
is sufficient for brute-force / abuse dampening (not billing-accurate fairness).

The fixed window (reset on rollover, not a sliding window) is deliberate: it is
simpler, allocates less, and is adequate for this threat model.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

logger = logging.getLogger("argus.ratelimit")


class _WindowCounter:
    """Fixed-window counter per key (client IP).

    Not thread-safe across processes (single in-memory dict); within one asyncio
    process ``check`` is synchronous (no ``await`` between read and write), so
    concurrent coroutines cannot interleave inside it.
    """

    def __init__(
        self,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._clock = clock
        # key -> (window_start_monotonic, count)
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str, limit: int) -> tuple[bool, int, int]:
        """Increment and evaluate the window for ``key``.

        Returns ``(allowed, count_after_increment, retry_after_s)``.
        ``retry_after_s`` is the remaining window time in whole seconds (min 1);
        it is only meaningful when ``allowed`` is False (the middleware ignores
        it otherwise).
        """
        now = self._clock()
        window_start, count = self._windows.get(key, (0.0, 0))
        if now - window_start >= self._window_s:
            window_start = now
            count = 0
        count += 1
        self._windows[key] = (window_start, count)

        allowed = count <= limit
        remaining = self._window_s - (now - window_start)
        retry_after = max(1, int(remaining) + (0 if remaining.is_integer() else 1))
        return allowed, count, retry_after


class RateLimitMiddleware:
    """ASGI middleware applying the per-IP fixed-window limiter.

    No-op when ``Settings.rate_limit_enabled`` is False — the request is handed
    straight through with no counter work. ``/health`` is always exempt
    (readiness-probe contract in ``routes/health.py``). Over-limit requests get
    a transport-level 429 with ``Retry-After``, not a ``{ok:false,...}`` body
    (consistent with FastAPI's own 401/422/429 rendering).
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        self._counter = _WindowCounter(settings.rate_limit_window_s)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if not self._settings.rate_limit_enabled:
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "unknown"
        allowed, count, retry_after_s = self._counter.check(
            ip, self._settings.rate_limit_requests
        )
        if allowed:
            await self._app(scope, receive, send)
            return

        logger.info(
            "rate limit exceeded ip=%s requests=%d window=%d path=%s",
            ip,
            count,
            self._settings.rate_limit_window_s,
            path,
        )
        await self._send_429(send, retry_after_s)

    @staticmethod
    async def _send_429(send: Send, retry_after: int) -> None:
        body = b'{"detail":"rate limit exceeded"}'
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})