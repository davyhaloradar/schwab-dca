"""httpx client for Charles Schwab Trader + Market Data APIs."""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)

# Access tokens typically last ~30 minutes; refresh ~7 days.
ACCESS_TOKEN_TTL = timedelta(minutes=28)
REFRESH_TOKEN_TTL = timedelta(days=7)
REFRESH_SKEW = timedelta(minutes=2)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _basic_auth_header(app_key: str, app_secret: str) -> str:
    raw = f"{app_key}:{app_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def build_authorize_url() -> str:
    settings = get_settings()
    params = {
        "client_id": settings.schwab_app_key,
        "redirect_uri": settings.schwab_callback_url,
        "response_type": "code",
    }
    return f"{settings.schwab_authorize_url}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange authorization code for tokens and persist encrypted."""
    settings = get_settings()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.schwab_callback_url,
    }
    headers = {
        "Authorization": _basic_auth_header(settings.schwab_app_key, settings.schwab_app_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(settings.schwab_token_url, data=data, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    _persist_token_response(payload)
    return payload


def refresh_access_token(refresh_token: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    if refresh_token is None:
        tokens = db.get_tokens()
        if not tokens:
            raise RuntimeError("No refresh token stored")
        refresh_token = tokens["refresh_token"]
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(settings.schwab_app_key, settings.schwab_app_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(settings.schwab_token_url, data=data, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    _persist_token_response(payload, prior_refresh=refresh_token)
    return payload


def _persist_token_response(payload: dict[str, Any], prior_refresh: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    expires_in = int(payload.get("expires_in") or 1800)
    access_exp = (now + timedelta(seconds=expires_in)).isoformat()
    # Schwab refresh tokens last ~7 days from issuance / last refresh
    refresh_exp = (now + REFRESH_TOKEN_TTL).isoformat()
    access = payload["access_token"]
    refresh = payload.get("refresh_token") or prior_refresh
    if not refresh:
        raise RuntimeError("Token response missing refresh_token")
    db.save_tokens(
        access,
        refresh,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_exp,
        scope=payload.get("scope"),
        token_type=payload.get("token_type"),
    )


def auth_status() -> dict[str, Any]:
    """UI-friendly OAuth status (no raw tokens)."""
    settings = get_settings()
    tokens = db.get_tokens()
    now = datetime.now(timezone.utc)
    if not tokens:
        return {
            "linked": False,
            "oauth_configured": settings.oauth_configured,
            "access_valid": False,
            "refresh_valid": False,
            "access_expires_at": None,
            "refresh_expires_at": None,
            "message": "未连接 / Not linked",
        }
    access_exp = _parse_iso(tokens.get("access_expires_at"))
    refresh_exp = _parse_iso(tokens.get("refresh_expires_at"))
    access_valid = bool(access_exp and access_exp > now + REFRESH_SKEW)
    refresh_valid = bool(refresh_exp and refresh_exp > now)
    return {
        "linked": True,
        "oauth_configured": settings.oauth_configured,
        "access_valid": access_valid,
        "refresh_valid": refresh_valid,
        "access_expires_at": tokens.get("access_expires_at"),
        "refresh_expires_at": tokens.get("refresh_expires_at"),
        "updated_at": tokens.get("updated_at"),
        "message": (
            "已连接 / Linked"
            if refresh_valid
            else "Refresh token 已过期，请重新登录 / Refresh expired — re-auth required"
        ),
    }


def get_valid_access_token() -> str:
    """Return a usable access token, refreshing if near expiry."""
    tokens = db.get_tokens()
    if not tokens:
        raise RuntimeError("Not authenticated — visit /auth/login")
    now = datetime.now(timezone.utc)
    access_exp = _parse_iso(tokens.get("access_expires_at"))
    refresh_exp = _parse_iso(tokens.get("refresh_expires_at"))
    if refresh_exp and refresh_exp <= now:
        raise RuntimeError("Refresh token expired (~7 days) — re-authenticate via /auth/login")
    if access_exp and access_exp > now + REFRESH_SKEW:
        return tokens["access_token"]
    logger.info("Access token near expiry; refreshing")
    payload = refresh_access_token(tokens["refresh_token"])
    return payload["access_token"]


def _auth_headers() -> dict[str, str]:
    token = get_valid_access_token()
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_account_numbers() -> list[dict[str, Any]]:
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts/accountNumbers"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers())
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("accounts") or data.get("accountNumbers") or []


def extract_hash_from_account(entry: dict[str, Any] | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in ("hashValue", "accountHash", "hash"):
        val = entry.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def first_account_hash() -> str | None:
    """First (or sole) linked account hashValue from Trader API. None if unlinked/empty."""
    status = auth_status()
    if not status.get("linked") or not status.get("refresh_valid"):
        return None
    try:
        nums = get_account_numbers()
    except Exception as exc:
        logger.warning("first_account_hash: get_account_numbers failed: %s", exc)
        return None
    if not nums:
        return None
    return extract_hash_from_account(nums[0])


def get_accounts(fields: str | None = "positions") -> list[dict[str, Any]]:
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts"
    params = {"fields": fields} if fields else None
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list):
        return data
    return []



def get_account(account_hash: str, fields: str | None = None) -> dict[str, Any]:
    """GET /accounts/{accountHash} — balances / positions for one account."""
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts/{account_hash}"
    params = {"fields": fields} if fields else None
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


# Fields that are CASH-ONLY for DCA gating on MARGIN accounts.
# Never treat buyingPower / marginBuyingPower / dayTradingBuyingPower /
# availableFunds / liquidationValue as "cash" — those include margin capacity
# or total equity and would let DCA spend on margin.
CASH_ONLY_BALANCE_KEYS: tuple[str, ...] = (
    # Preferred: cash ready for new trades after pending activity
    "cashAvailableForTrading",
    # Settled + unsettled cash on hand (excludes loan capacity)
    "cashBalance",
    # Cash earmarked for non-marginable trades (equities often fall here)
    "availableFundsNonMarginableTrade",
)

# Explicitly excluded from DCA cash gate (documented for audits / UI)
MARGIN_OR_EQUITY_KEYS: frozenset[str] = frozenset({
    "buyingPower",
    "marginBuyingPower",
    "dayTradingBuyingPower",
    "availableFunds",  # on margin accounts includes BP
    "liquidationValue",  # total equity, not spendable cash
    "equity",
    "buyingPowerNonMarginableTrade",  # can still reflect non-cash BP semantics
})


def _balances_block(account_payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Return (balances_dict, which_block) from a Schwab account JSON."""
    if not isinstance(account_payload, dict):
        return {}, None
    sec = account_payload.get("securitiesAccount") or account_payload
    if not isinstance(sec, dict):
        return {}, None
    for name in ("currentBalances", "projectedBalances", "initialBalances"):
        block = sec.get(name)
        if isinstance(block, dict) and block:
            return block, name
    return {}, None


def _float_or_none(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def extract_cash_details(account_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Margin-safe cash for DCA.

    Prefers cashAvailableForTrading → cashBalance → availableFundsNonMarginableTrade
    from currentBalances (then projected / initial). Never uses buyingPower /
    marginBuyingPower / dayTradingBuyingPower / availableFunds / liquidationValue.

    Returns dict with:
      cash: float | None
      field_used: str | None
      balances_block: str | None
      cash_fields_seen: dict[str, float]
      excluded_margin_fields_seen: dict[str, float]
      account_type: str | None
    """
    out: dict[str, Any] = {
        "cash": None,
        "field_used": None,
        "balances_block": None,
        "cash_fields_seen": {},
        "excluded_margin_fields_seen": {},
        "account_type": None,
    }
    if not isinstance(account_payload, dict):
        return out
    sec = account_payload.get("securitiesAccount") or account_payload
    if isinstance(sec, dict):
        out["account_type"] = sec.get("type") or sec.get("accountType")
    balances, block_name = _balances_block(account_payload)
    out["balances_block"] = block_name
    if not balances:
        return out

    cash_seen: dict[str, float] = {}
    for key in CASH_ONLY_BALANCE_KEYS:
        f = _float_or_none(balances.get(key))
        if f is not None:
            cash_seen[key] = f
    out["cash_fields_seen"] = cash_seen

    excluded: dict[str, float] = {}
    for key in MARGIN_OR_EQUITY_KEYS:
        f = _float_or_none(balances.get(key))
        if f is not None:
            excluded[key] = f
    out["excluded_margin_fields_seen"] = excluded

    for key in CASH_ONLY_BALANCE_KEYS:
        if key not in cash_seen:
            continue
        # Allow 0 (empty cash); negative cashBalance means debit — treat as 0 spendable
        cash = cash_seen[key]
        spendable = cash if cash >= 0 else 0.0
        out["cash"] = spendable
        out["field_used"] = key
        if cash < 0:
            out["raw_negative_cash"] = cash
        return out
    return out


def extract_cash_available(account_payload: dict[str, Any]) -> float | None:
    """Margin-safe cash amount only (see extract_cash_details). Never margin BP."""
    return extract_cash_details(account_payload).get("cash")


def get_cash_details(account_hash: str) -> dict[str, Any]:
    """Fetch account and return margin-safe cash details. cash=None on API failure."""
    try:
        payload = get_account(account_hash)
    except Exception as exc:
        logger.warning("get_cash_details failed for %s…: %s", account_hash[:8], exc)
        return {
            "cash": None,
            "field_used": None,
            "balances_block": None,
            "cash_fields_seen": {},
            "excluded_margin_fields_seen": {},
            "account_type": None,
            "error": str(exc),
        }
    details = extract_cash_details(payload)
    details["account_hash_prefix"] = account_hash[:8]
    return details


def get_cash_available(account_hash: str) -> float | None:
    """Fetch margin-safe cash for account_hash (never buyingPower). None if unavailable."""
    return get_cash_details(account_hash).get("cash")


def place_order(account_hash: str, order: dict[str, Any]) -> dict[str, Any]:
    """POST equity order. Success is typically 201 with Location header (no body)."""
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts/{account_hash}/orders"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, headers=headers, json=order)
        if resp.status_code not in (200, 201):
            detail = resp.text
            try:
                detail = resp.json()
            except Exception:
                pass
            raise RuntimeError(f"Order failed HTTP {resp.status_code}: {detail}")
        location = resp.headers.get("Location") or resp.headers.get("location")
        order_id = None
        if location:
            order_id = location.rstrip("/").split("/")[-1]
        return {
            "status_code": resp.status_code,
            "location": location,
            "order_id": order_id,
            "body": resp.text or None,
        }


def get_quotes(symbols: list[str]) -> dict[str, Any]:
    """
    Fetch quotes from Market Data API.
    Endpoint: GET /marketdata/v1/quotes?symbols=VOO,QQQ
    If the call fails, caller should fall back to placeholder pricing for dry-run.
    """
    settings = get_settings()
    url = f"{settings.schwab_marketdata_base}/quotes"
    params = {"symbols": ",".join(symbols), "fields": "quote,reference"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


def extract_last_price(quote_payload: dict[str, Any], symbol: str) -> float | None:
    """Best-effort last/mark price from Schwab quote JSON."""
    sym = symbol.upper()
    entry = quote_payload.get(sym) or quote_payload.get(symbol)
    if not entry and isinstance(quote_payload, dict):
        # sometimes nested under symbol key variants
        for k, v in quote_payload.items():
            if k.upper() == sym and isinstance(v, dict):
                entry = v
                break
    if not isinstance(entry, dict):
        return None
    q = entry.get("quote") or entry
    for key in ("lastPrice", "mark", "regularMarketLastPrice", "closePrice", "askPrice", "bidPrice"):
        val = q.get(key) if isinstance(q, dict) else None
        if val is not None:
            try:
                price = float(val)
                if price > 0:
                    return price
            except (TypeError, ValueError):
                continue
    return None


def build_equity_market_buy(symbol: str, quantity: int) -> dict[str, Any]:
    """
    Schwab Trader API equity MARKET BUY by whole-share quantity.
    Dollar-amount orders are NOT supported for equities; convert USD → floor(shares).
    """
    if quantity < 1:
        raise ValueError(f"Quantity must be >= 1 whole shares, got {quantity}")
    return {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": quantity,
                "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
            }
        ],
    }


def get_order(account_hash: str, order_id: str) -> dict[str, Any]:
    """GET /accounts/{accountHash}/orders/{orderId}"""
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts/{account_hash}/orders/{order_id}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()


def cancel_order(account_hash: str, order_id: str) -> dict[str, Any]:
    """DELETE /accounts/{accountHash}/orders/{orderId} — cancel working order."""
    settings = get_settings()
    url = f"{settings.schwab_trader_base}/accounts/{account_hash}/orders/{order_id}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.delete(url, headers=_auth_headers())
        if resp.status_code not in (200, 201, 204):
            detail = resp.text
            try:
                detail = resp.json()
            except Exception:
                pass
            raise RuntimeError(f"Cancel failed HTTP {resp.status_code}: {detail}")
        return {
            "status_code": resp.status_code,
            "order_id": order_id,
            "body": resp.text or None,
        }


# Statuses that typically still allow cancel (Schwab / TD Ameritrade style).
CANCELABLE_STATUSES = frozenset({
    "AWAITING_PARENT_ORDER",
    "AWAITING_CONDITION",
    "AWAITING_STOP_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "ACCEPTED",
    "AWAITING_UR_OUT",
    "PENDING_ACTIVATION",
    "QUEUED",
    "WORKING",
    "NEW",
    "AWAITING_RELEASE_TIME",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_RECALL",
})



def _average_from_activities(order: dict[str, Any]) -> float | None:
    """Best-effort average fill from orderActivityCollection execution legs."""
    activities = order.get("orderActivityCollection") or []
    total_qty = 0.0
    total_notional = 0.0
    for act in activities:
        if not isinstance(act, dict):
            continue
        for leg in act.get("executionLegs") or []:
            if not isinstance(leg, dict):
                continue
            try:
                p = float(leg.get("price"))
                q = float(leg.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if q <= 0 or p <= 0:
                continue
            total_qty += q
            total_notional += p * q
    if total_qty <= 0:
        return None
    return round(total_notional / total_qty, 6)


def summarize_order(order: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Schwab order JSON for the Run Log UI."""
    status = order.get("status") or order.get("orderStatus") or "UNKNOWN"
    legs = order.get("orderLegCollection") or []
    symbols = []
    qty = None
    instruction = None
    for leg in legs:
        inst = leg.get("instrument") or {}
        if inst.get("symbol"):
            symbols.append(inst["symbol"])
        if qty is None and leg.get("quantity") is not None:
            qty = leg.get("quantity")
        if instruction is None:
            instruction = leg.get("instruction")
    filled = order.get("filledQuantity")
    remaining = order.get("remainingQuantity")
    avg = order.get("averagePrice")
    try:
        avg_f = float(avg) if avg is not None and avg != "" else None
    except (TypeError, ValueError):
        avg_f = None
    if avg_f is None or avg_f <= 0:
        # Fall back: weighted avg from execution legs when top-level averagePrice missing
        avg = _average_from_activities(order)
    else:
        avg = avg_f
    return {
        "order_id": str(order.get("orderId") or order.get("order_id") or ""),
        "status": status,
        "cancelable": status.upper() in CANCELABLE_STATUSES,
        "symbols": symbols,
        "instruction": instruction,
        "quantity": qty,
        "filled_quantity": filled,
        "remaining_quantity": remaining,
        "order_type": order.get("orderType"),
        "entered_time": order.get("enteredTime") or order.get("enteredTimeUTC"),
        "close_time": order.get("closeTime"),
        "price": order.get("price") or order.get("stopPrice"),
        "average_price": avg,
    }
