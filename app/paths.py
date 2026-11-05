"""URL path helpers for APP_ROOT_PATH (e.g. /plan)."""
from __future__ import annotations

from fastapi.responses import RedirectResponse

from app.config import get_settings


def bp() -> str:
    """Base path without trailing slash, e.g. \"/plan\" or \"\"."""
    return (get_settings().app_root_path or "").rstrip("/")


def href(path: str = "/") -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{bp()}{path}"


def redirect(path: str = "/", *, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(href(path), status_code=status_code)
