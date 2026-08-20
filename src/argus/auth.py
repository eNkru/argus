"""Bearer-token authentication for all ``/v1/*`` routes.

Compares the ``Authorization: Bearer <token>`` header against each valid token
in ``Settings.api_token_set`` using ``secrets.compare_digest`` (constant-time),
so a timing oracle can't be used to recover a token byte-by-byte. Multiple
tokens are supported (rotation / per-client issuance).

``ARGUS_AUTH_DISABLED=true`` bypasses the check entirely for local dev.
``/health`` is unauthenticated (see ``routes/health.py``).
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_settings(request: Request) -> Settings:
    """Read the singleton Settings stashed on app.state by the lifespan."""
    return request.app.state.settings


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """FastAPI dependency gating /v1/* routes behind a valid bearer token."""
    settings = _get_settings(request)
    if settings.auth_disabled:
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or non-bearer Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    for valid in settings.api_token_set:
        if secrets.compare_digest(token, valid):
            return

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
