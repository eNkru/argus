"""Cookie normalization.

The ``Cookie`` pydantic model already matches the dict shape that the
``BrowserContext`` yielded by ``AsyncCamoufox`` accepts via ``add_cookies(...)``,
so normalization is a plain ``model_dump()``. Kept as a dedicated module so the
route handlers stay clean and any future normalization (coercing alternate input
shapes, validating magic bytes, logging) has a home.
"""

from __future__ import annotations

from .schemas import Cookie


def to_playwright_cookies(cookies: list[Cookie]) -> list[dict]:
    """Convert validated cookies to the dict shape ``add_cookies`` expects."""
    return [c.model_dump() for c in cookies]
