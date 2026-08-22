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

    # --- AI price-extraction fallback (POST /v1/extract-price stage 2). ---
    # Generic OpenAI-compatible chat-completions provider (no `openai` SDK);
    # shape mirrors iris's env so an operator can point both services at the
    # same provider. ALL THREE of base_url + api_key + model must be non-empty
    # for the AI stage to exist; leaving any empty disables the LLM stage
    # entirely and /v1/extract-price degrades to a JSON-LD-only service.

    # OpenAI-compatible base URL, e.g. "https://opencode.ai/zen/v1".
    ai_base_url: str = ""

    # SECRET — the provider API key. Never logged (logging-guidelines.md); the
    # only log lines about it are value-free ("AI provider not configured").
    ai_api_key: str = ""

    # Chat model name, e.g. a DeepSeek free-tier id.
    ai_model: str = ""

    # Optional Zen parity headers: when urlparse(ai_base_url).hostname equals
    # ai_zen_host, ai_user_agent and ai_client_header ride on the request so
    # argus and iris can share one free-tier provider's allowlist.
    ai_zen_host: str = ""
    ai_user_agent: str = ""
    ai_client_header: str = ""

    # Max concurrent LLM calls (asyncio semaphore). Conservative 1 mirrors
    # iris's default — free tiers 429 quickly under bursts.
    ai_concurrency: int = 1

    # Minimum gap between LLM calls, ms. Read live on every call (unlike
    # concurrency, which is captured once at first use).
    ai_min_interval_ms: int = 200

    # Retry attempts for transient provider errors (429/502/503/504 only).
    ai_max_retries: int = 3

    @property
    def api_token_set(self) -> set[str]:
        """Valid bearer tokens, whitespace-trimmed, empties dropped."""
        return {t.strip() for t in self.api_tokens.split(",") if t.strip()}
