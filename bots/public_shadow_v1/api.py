"""bots/public_shadow_v1/api.py — minimal Public.com client for account #2.

Mirrors the auth pattern in `league_core.public_bars` but uses a SEPARATE
set of credentials (PUBLIC_SECRET_ACCOUNT2 / PUBLIC_ACCOUNT_ID_ACCOUNT2)
so this code only ever touches the new brokerage account, never the
existing live-capital account.

Two endpoints we care about:

  POST /userapiauthservice/personal/access-tokens     — auth
  GET  /userapigateway/trading/{accountId}/portfolio  — history & positions

Response shape is documented at https://public.com/api/docs but we
defensively coerce everything because Public's API has occasionally
returned partial shapes during our experience with the live bots.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests


PUBLIC_AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
PUBLIC_HISTORY_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/history"
)
PUBLIC_PORTFOLIO_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/portfolio-v2"
)

DEFAULT_TIMEOUT = 15.0

_session = requests.Session()
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _redacted(resp: requests.Response) -> str:
    try:
        body = resp.text[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001
        body = "<unreadable>"
    return f"status={resp.status_code} body[:200]={body!r}"


def get_access_token(
    secret: Optional[str] = None,
    validity_minutes: int = 60,
    force_refresh: bool = False,
) -> Optional[str]:
    """Fetch (and cache) an access token. Prefers PUBLIC_SECRET_ACCOUNT2 if
    set (in case you ever issue a separate key per account), falls back to
    the existing PUBLIC_SECRET so you don't have to duplicate the secret
    when both accounts live under one Public login + key.

    The account is disambiguated downstream by PUBLIC_ACCOUNT_ID_ACCOUNT2,
    NOT by the secret. The secret just authenticates "you"; the account
    ID picks which of your accounts to act on.

    Returns None on failure (caller treats as "skip cycle, retry next time").
    """
    if secret is None:
        secret = (os.getenv("PUBLIC_SECRET_ACCOUNT2")
                  or os.getenv("PUBLIC_SECRET")
                  or "")
    if not secret:
        print("[public_shadow] neither PUBLIC_SECRET_ACCOUNT2 nor PUBLIC_SECRET set — idle")
        return None

    now = time.time()
    if (not force_refresh
            and _token_cache.get("token")
            and now < float(_token_cache.get("expires_at") or 0.0)):
        return str(_token_cache["token"])

    try:
        resp = _session.post(
            PUBLIC_AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={"secret": secret, "validityInMinutes": int(validity_minutes)},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[public_shadow] auth request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"[public_shadow] auth failed {_redacted(resp)}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[public_shadow] auth returned non-JSON {_redacted(resp)}")
        return None

    token = data.get("accessToken")
    if not token:
        print(f"[public_shadow] auth missing accessToken (keys={list(data.keys())})")
        return None

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(60.0, (validity_minutes - 2) * 60.0)
    return str(token)


def fetch_history(
    account_id: Optional[str] = None,
    *,
    token: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch recent trade history for the account. Returns a list of dicts
    (one per trade) or None on hard failure. Caller should treat None as
    "skip this cycle."

    The response shape we expect (defensively coerced) per Public's docs:

        {
          "history": [
            {
              "orderId": "...",
              "instrument": {"symbol": "AAPL", "type": "EQUITY", ...},
              "side": "BUY"|"SELL"|...,
              "quantity": 0.5,
              "filledPrice": 213.50,
              "amount": 106.75,
              "fees": 0.0,
              "createdAt": "2026-05-21T20:33:00Z",
              ...
            },
            ...
          ]
        }

    Real shapes have varied over time. We tolerate missing fields and
    pick best-available alternatives.
    """
    account_id = account_id or os.getenv("PUBLIC_ACCOUNT_ID_ACCOUNT2", "")
    if not account_id:
        print("[public_shadow] PUBLIC_ACCOUNT_ID_ACCOUNT2 not set — bot will idle")
        return None

    if token is None:
        token = get_access_token()
        if token is None:
            return None

    url = PUBLIC_HISTORY_URL_TMPL.format(account_id=account_id)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = _session.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        print(f"[public_shadow] history request failed: {e}")
        return None

    # 401 → retry once with a fresh token.
    if resp.status_code == 401:
        token = get_access_token(force_refresh=True)
        if token is None:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = _session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            print(f"[public_shadow] history retry failed: {e}")
            return None

    if resp.status_code != 200:
        print(f"[public_shadow] history failed {_redacted(resp)}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[public_shadow] history returned non-JSON {_redacted(resp)}")
        return None

    # Public's response keys have varied: 'history', 'trades', or top-level list.
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("history") or data.get("trades") or data.get("transactions") or []
    else:
        rows = []

    if not isinstance(rows, list):
        print(f"[public_shadow] unexpected history payload type: {type(rows).__name__}")
        return None

    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  Shape normalization — Public's API has shifted shapes over time. Normalize
#  every trade-like object into a consistent dict the rest of the bot can
#  trust. Missing fields become None (not zero) so the dashboard distinguishes
#  "unknown" from "actually zero."
# ─────────────────────────────────────────────────────────────────────────────

_SIDE_MAP = {
    "BUY":    "BUY",
    "SELL":   "SELL",
    "SHORT":  "SHORT",
    "COVER":  "COVER",
    # Crypto sometimes uses different verbs:
    "BUY_TO_OPEN":   "BUY",
    "SELL_TO_CLOSE": "SELL",
    "SELL_TO_OPEN":  "SHORT",
    "BUY_TO_CLOSE":  "COVER",
}

_CLASS_MAP = {
    "EQUITY":   "equity",
    "ETF":      "etf",
    "CRYPTO":   "crypto",
    "BOND":     "bond",
    "OPTION":   "option",
    "TREASURY": "bond",  # Public buckets treasuries under fixed-income / bonds
}


def _coerce_number(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_trade(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a Public history entry into the shape we want to write to
    bot_trades. Returns None if the entry is too incomplete to be useful
    (e.g., no order_id AND no timestamp — can't dedup or order it).
    """
    if not isinstance(raw, dict):
        return None

    order_id = raw.get("orderId") or raw.get("order_id") or raw.get("id")
    instrument = raw.get("instrument") or {}
    if isinstance(instrument, dict):
        symbol = instrument.get("symbol") or raw.get("symbol")
        inst_type = (instrument.get("type") or instrument.get("assetClass")
                     or raw.get("assetClass") or "EQUITY")
    else:
        symbol = raw.get("symbol")
        inst_type = raw.get("assetClass") or "EQUITY"

    if not symbol:
        return None

    side_raw = (raw.get("side") or raw.get("action") or "").upper()
    side = _SIDE_MAP.get(side_raw)
    if side is None:
        return None  # Skip status updates / non-trade entries

    asset_class = _CLASS_MAP.get((inst_type or "").upper(), "equity")

    quantity = _coerce_number(raw.get("quantity") or raw.get("filledQuantity"))
    price = _coerce_number(raw.get("filledPrice") or raw.get("price")
                           or raw.get("averageFillPrice"))
    amount = _coerce_number(raw.get("amount") or raw.get("notional")
                            or raw.get("filledAmount"))
    fees = _coerce_number(raw.get("fees") or raw.get("totalFees")) or 0.0
    occurred = (raw.get("createdAt") or raw.get("filledAt")
                or raw.get("timestamp") or raw.get("occurredAt"))

    if not occurred and not order_id:
        return None  # Can't dedup or sort it; safer to skip.

    return {
        "order_id":    str(order_id) if order_id else None,
        "occurred_at": occurred,
        "symbol":      symbol.upper(),
        "asset_class": asset_class,
        "side":        side,
        "quantity":    quantity,
        "price":       price,
        "amount_usd":  amount,
        "fees_usd":    fees,
    }
