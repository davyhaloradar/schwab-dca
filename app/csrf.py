"""CSRF (double-submit cookie) + same-origin checks for mutating POSTs."""
from __future__ import annotations

import logging
import secrets
from urllib.parse import parse_qs, urlparse

from starlette.datastructures import FormData
from starlette.formparsers import FormParser, MultiPartParser
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.config import get_settings
from app.paths import bp

logger = logging.getLogger(__name__)

CSRF_COOKIE = "csrf_token"
CSRF_FORM_FIELD = "_csrf"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _host_allowed(host: str | None) -> bool:
    """Allowlist comes from ALLOWED_HOSTS, or from the callback URL when unset."""
    return get_settings().host_allowed(host)


def same_origin_ok(request: Request) -> bool:
    """If Origin or Referer is present, require same-host; absent → OK (CSRF cookie still required)."""
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()
    if not origin and not referer:
        return True
    for header_val in (origin, referer):
        if not header_val:
            continue
        try:
            parsed = urlparse(header_val)
        except Exception:
            return False
        if not _host_allowed(parsed.hostname):
            return False
    return True


def ensure_request_csrf(request: Request) -> str:
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and len(existing) >= 16:
        token = existing
    else:
        token = new_csrf_token()
    request.state.csrf_token = token
    return token


def _under_app(path: str) -> bool:
    prefix = bp() or ""
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def _csrf_from_urlencoded(body: bytes) -> str | None:
    try:
        qs = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    except Exception:
        return None
    vals = qs.get(CSRF_FORM_FIELD)
    if not vals:
        return None
    return vals[0]


async def _csrf_from_multipart(request: Request, body: bytes) -> str | None:
    content_type = request.headers.get("content-type") or ""
    try:
        parser = MultiPartParser(headers=request.headers, stream=_async_bytes(body))
        form: FormData = await parser.parse()
    except Exception:
        # Fall back: naive search (not for binary fields)
        marker = f'name="{CSRF_FORM_FIELD}"'.encode()
        idx = body.find(marker)
        if idx < 0:
            return None
        # After headers blank line, value until next boundary
        rest = body[idx:]
        nl = rest.find(b"\r\n\r\n")
        if nl < 0:
            return None
        val = rest[nl + 4 :]
        end = val.find(b"\r\n")
        if end < 0:
            return None
        return val[:end].decode("utf-8", errors="replace")
    raw = form.get(CSRF_FORM_FIELD)
    return raw if isinstance(raw, str) else None


async def _async_bytes(data: bytes):
    yield data


def _want_secure_cookie(request: Request) -> bool:
    xf = (request.headers.get("x-forwarded-proto") or "").lower()
    if xf == "https":
        return True
    if xf == "http":
        return False
    return request.url.scheme == "https"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Set csrf cookie on responses; require matching _csrf + same-origin on mutating POSTs."""

    async def dispatch(self, request: Request, call_next):
        token = ensure_request_csrf(request)
        method = request.method.upper()
        replay_body: bytes | None = None

        if method in ("POST", "PUT", "PATCH", "DELETE") and _under_app(request.url.path):
            if not same_origin_ok(request):
                logger.warning("CSRF/origin rejected %s %s", method, request.url.path)
                return PlainTextResponse("Forbidden: bad Origin/Referer", status_code=403)

            # Buffer body so we can validate CSRF then replay for FastAPI Form()
            replay_body = await request.body()
            content_type = (request.headers.get("content-type") or "").lower()
            form_tok: str | None = None
            if "application/x-www-form-urlencoded" in content_type:
                form_tok = _csrf_from_urlencoded(replay_body)
            elif "multipart/form-data" in content_type:
                form_tok = await _csrf_from_multipart(request, replay_body)
            else:
                # Non-form POSTs under app still require cookie match via empty → reject
                form_tok = _csrf_from_urlencoded(replay_body)

            cookie_tok = request.cookies.get(CSRF_COOKIE) or ""
            if not cookie_tok or not form_tok or not secrets.compare_digest(cookie_tok, form_tok):
                logger.warning("CSRF token mismatch on %s %s", method, request.url.path)
                return PlainTextResponse(
                    "Forbidden: CSRF token missing or invalid", status_code=403
                )

            async def receive():
                return {"type": "http.request", "body": replay_body, "more_body": False}

            request = Request(request.scope, receive)

        response = await call_next(request)
        secure = _want_secure_cookie(request)
        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=secure,
            path=bp() or "/",
            max_age=60 * 60 * 24 * 7,
        )
        return response


def csrf_input_html(request: Request) -> str:
    """Jinja helper: hidden input markup."""
    tok = getattr(request.state, "csrf_token", None) or request.cookies.get(CSRF_COOKIE) or ""
    # Escape quotes in token (token is urlsafe so alphanumeric/_/- only)
    safe = tok.replace('"', "")
    return f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{safe}" />'
