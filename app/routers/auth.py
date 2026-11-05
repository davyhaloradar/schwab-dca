"""OAuth login / callback / logout."""
from __future__ import annotations

import logging
from urllib.parse import unquote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import db
from app.config import get_settings
from app.paths import href, redirect
from app.services import schwab_client

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/auth/login")
async def auth_login():
    settings = get_settings()
    if not settings.oauth_configured:
        return HTMLResponse(
            "<h1>OAuth not configured</h1>"
            "<p>Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env</p>"
            f'<p><a href="{href()}">Back</a></p>',
            status_code=400,
        )
    from fastapi.responses import RedirectResponse as _RR
    return _RR(schwab_client.build_authorize_url(), status_code=302)


@router.get("/callback")
async def oauth_callback(
    code: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    if error:
        msg = error_description or error
        return HTMLResponse(
            f"<h1>OAuth error</h1><pre>{msg}</pre><p><a href='{href()}'>Back</a></p>",
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            f"<h1>Missing code</h1><p><a href='{href()}'>Back</a></p>",
            status_code=400,
        )
    # Schwab may URL-encode the code
    code = unquote(code)
    try:
        schwab_client.exchange_code(code)
    except Exception as exc:
        logger.exception("Token exchange failed")
        return HTMLResponse(
            f"<h1>Token exchange failed</h1><pre>{exc}</pre><p><a href='{href()}'>Back</a></p>",
            status_code=500,
        )
    return redirect("/", status_code=302)


@router.post("/auth/logout")
async def auth_logout():
    db.clear_tokens()
    return redirect("/", status_code=303)


@router.get("/auth/status")
async def auth_status_api():
    return schwab_client.auth_status()
