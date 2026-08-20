"""Environment-driven configuration (pydantic-settings).

All env vars are prefixed ``ARGUS_``. Settings are constructed once in
``main.lifespan`` and stashed on ``app.state.settings``; route handlers and the
auth dependency read them from there rather than re-parsing env per request.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bearer tokens accepted on /v1/* routes. Stored as a single comma-separated
    # string (pydantic-settings would otherwise try JSON-parsing a list) and
    # split on demand below.
    api_tokens: str = ""

    # Bypass bearer auth entirely (local dev only).
    auth_disabled: bool = False

    # Shared browser concurrency (asyncio semaphore bounding concurrent fetches).
    concurrency: int = 5

    # Idle teardown: close the browser after this many seconds with no fetch.
    idle_timeout_seconds: float = 300.0

    # Per-request navigation timeout, seconds. Kept below the caller's own
    # AbortSignal timeout so the caller owns the outer envelope.
    fetch_timeout_seconds: float = 35.0

    # Post-domcontentloaded SPA render-wait cap, seconds.
    render_wait_seconds: float = 8.0

    # Camoufox fingerprint OS. Pin to the OS whose fonts are physically present
    # in the image (linux under the provided Dockerfile which prunes macos/windows).
    default_fingerprint_os: str = "linux"

    @property
    def api_token_set(self) -> set[str]:
        """Valid bearer tokens, whitespace-trimmed, empties dropped."""
        return {t.strip() for t in self.api_tokens.split(",") if t.strip()}
