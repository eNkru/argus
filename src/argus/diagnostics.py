"""Consecutive-fetch-failure diagnostics.

Counts and logs fetch failures with diagnostic detail so a degrading browser
reads as a trend in the logs, not isolated per-request warnings. Exactly once
when the count crosses ``DIAGNOSE_THRESHOLD`` (3), emits a richer "browser
degraded" summary with a traceback to capture root cause. The counter resets on
any success — a single success clears the degradation trend.
"""

from __future__ import annotations

import logging
import traceback
from typing import Optional

logger = logging.getLogger("argus.diagnostics")

# Diagnostic threshold for shared-browser degradation. Aligned with the future
# self-heal trigger point so the logs are directly comparable. LOGGING-ONLY
# here: no recreation, no lock, no teardown.
DIAGNOSE_THRESHOLD = 3


class FailureTracker:
    def __init__(self) -> None:
        self._consecutive_failures: int = 0

    def record_success(self) -> None:
        """Reset the consecutive-failure counter on a successful fetch."""
        self._consecutive_failures = 0

    def record_failure(
        self, url: str, exc: BaseException | None, *, kind: str
    ) -> None:
        """Count and log a fetch failure with diagnostic detail.

        ``kind`` is a short category label ("timeout" | "error" | "no_response"
        | "cookie_injection") so the no-response path (which has no exception
        object) is still accounted. The rich "browser degraded" line is emitted
        exactly once when the count crosses the threshold and not re-emitted
        until the counter resets.
        """
        self._consecutive_failures += 1
        count = self._consecutive_failures
        error_type = _exc_type_name(exc) if exc is not None else kind
        message = str(exc) if exc is not None else "page.goto returned no response"
        logger.warning(
            "fetch %s url=%s error_type=%s error=%s consecutive_failures=%d",
            kind,
            url,
            error_type,
            message,
            count,
        )
        if count == DIAGNOSE_THRESHOLD and exc is not None:
            traceback_text = _traceback_repr(exc)
            logger.warning(
                "browser degraded — %d consecutive failures url=%s "
                "error_type=%s error=%s traceback=%s",
                count,
                url,
                error_type,
                message,
                traceback_text,
            )


def _exc_type_name(exc: BaseException) -> str:
    """Qualified exception class name for diagnostic logging."""
    cls = type(exc)
    module = getattr(cls, "__module__", "") or ""
    qualname = getattr(cls, "__qualname__", cls.__name__)
    return f"{module}.{qualname}" if module else qualname


def _traceback_repr(exc: BaseException) -> str:
    """Compact ``repr(exc)`` + traceback for the threshold diagnostic line."""
    return repr(exc) + "\n" + "".join(traceback.format_exception(exc))
