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

    # Interactive API docs (/docs /redoc /openapi.json). Default false (off in
    # production — the schema documents every endpoint + the auth scheme and
    # must not be public). dev.sh sets ARGUS_DOCS_ENABLED=true so local dev
    # keeps /docs (the README references it).
    docs_enabled: bool = False

    # Shared browser concurrency (asyncio semaphore bounding concurrent fetches).
    concurrency: int = 5

    # Idle teardown: close the browser after this many seconds with no fetch.
    idle_timeout_seconds: float = 300.0

    # Per-request navigation timeout, seconds. Kept below the caller's own
    # AbortSignal timeout so the caller owns the outer envelope.
    fetch_timeout_seconds: float = 35.0

    # Post-domcontentloaded SPA render-wait cap, seconds.
    render_wait_seconds: float = 8.0

    # SSRF guard: block fetched URLs whose host is private / loopback /
    # link-local / cloud-metadata (169.254.169.254, 127.0.0.1, RFC1918,
    # localhost, metadata.google.internal, ...). Default true (block) — the
    # secure default; a fetch service must not be an SSRF proxy. Operators who
    # legitimately scrape internal hosts set ARGUS_FETCH_ALLOW_PRIVATE=true to
    # bypass the guard entirely (escape hatch). See urlguard.py.
    fetch_allow_private: bool = False

    # Inbound request rate limiting (security): fixed-window per-IP throttle
    # applied by ratelimit.RateLimitMiddleware before auth — so the 401
    # bearer-brute-force path is throttled too. Default OFF (byte-identical
    # behavior until an operator opts in). When ON, over-limit requests get a
    # 429 + Retry-After; /health is always exempt. Single-process (in-memory).
    rate_limit_enabled: bool = False

    # Requests allowed per client IP per window. Generous: a single caller at
    # 1 req/s is 60/min, well under 600, so legitimate callers never trip it.
    rate_limit_requests: int = 600

    # Fixed-window length, seconds.
    rate_limit_window_s: int = 60

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

    # Optional retailer-domain allow-list for the AI fallback stage
    # (ARGUS_AI_EXTRACT_DOMAIN_ALLOWLIST). Comma-separated host suffixes, e.g.
    # "pbtech.co.nz,kogan.co.nz". Empty (default) = no restriction, so the AI
    # stage runs exactly as before (byte-identical behavior) — secure, opt-in
    # default. When non-empty, the AI stage in routes/extract.py runs only if
    # the fetched page's final hostname matches a listed suffix (exact or
    # ".suffix" subdomain, case-insensitive); otherwise it degrades to
    # extraction_failed with an INFO log — no LLM call, no cost. AI-extracted
    # prices are UNTRUSTED regardless (see security-guidelines.md); this only
    # scopes which hosts the LLM stage may run on.
    ai_extract_domain_allowlist: str = ""

    @property
    def api_token_set(self) -> set[str]:
        """Valid bearer tokens, whitespace-trimmed, empties dropped."""
        return {t.strip() for t in self.api_tokens.split(",") if t.strip()}

    @property
    def ai_extract_domain_allowlist_set(self) -> set[str]:
        """AI-stage host suffixes, whitespace-trimmed + lowercased, empties
        dropped. Empty set = no restriction (default = "")."""
        return {
            s.strip().lower()
            for s in self.ai_extract_domain_allowlist.split(",")
            if s.strip()
        }
