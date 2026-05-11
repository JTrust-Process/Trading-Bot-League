"""league_core/public_bars.py — Historical bars from Public.com.

Direct port of the pattern from `Trading Bot/Trading Bot Project/public_bars.py`
so new bots can use the exact same data source as the existing live stock bot.

Reference: https://public.com/api/docs/resources/market-data/get-bars-v2

This module is READ-ONLY. It never places orders. It exists so paper /
research bots can fetch the same OHLCV the live bot trains on.

Returns lightweight list-of-dicts rather than a pandas DataFrame so the
ETF rotation bot has no pandas dependency. Bots that need pandas can
wrap the output with `pd.DataFrame(bars)` themselves.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests


PUBLIC_AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
PUBLIC_BARS_URL_TMPL = (
    "https://api.public.com/userapigateway/historicdata/{symbol}/{period}"
)

VALID_PERIODS = {
    "DAY", "WEEK", "MONTH", "QUARTER", "HALF_YEAR",
    "YEAR", "FIVE_YEARS", "YTD", "SINCE_PURCHASE",
}

_NUMERIC_KEYS = ("open", "high", "low", "close", "volume", "value")
DEFAULT_TIMEOUT = 12.0

_session = requests.Session()
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _redacted_status(resp: requests.Response) -> str:
    try:
        body_excerpt = resp.text[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001
        body_excerpt = "<unreadable>"
    return f"status={resp.status_code} body[:200]={body_excerpt!r}"


def get_access_token(
    secret: Optional[str] = None,
    validity_minutes: int = 60,
    force_refresh: bool = False,
) -> Optional[str]:
    """Fetch (and cache) a Public.com access token. Reads PUBLIC_SECRET from env
    when `secret` is None. Returns None on failure."""
    secret = secret if secret is not None else os.getenv("PUBLIC_SECRET", "")
    if not secret:
        print("[public_bars] ERROR: PUBLIC_SECRET not set")
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
        print(f"[public_bars] auth request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"[public_bars] auth failed {_redacted_status(resp)}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[public_bars] auth returned non-JSON {_redacted_status(resp)}")
        return None

    token = data.get("accessToken")
    if not token:
        print(f"[public_bars] auth missing accessToken (keys={list(data.keys())})")
        return None

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(60.0, (validity_minutes - 2) * 60.0)
    return str(token)


def _coerce_bar(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {"timestamp": raw.get("timestamp")}
    for k in _NUMERIC_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    for k in ("open", "high", "low", "close"):
        if k not in out:
            return None
    out.setdefault("volume", 0.0)
    return out


def get_public_bars(
    symbol: str,
    period: str = "YEAR",
    *,
    token: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch historical bars for a symbol. Returns a list of dicts sorted
    oldest -> newest, each with keys: timestamp, open, high, low, close, volume.
    Returns None on hard failure, [] on a valid empty response.
    """
    if not symbol:
        print("[public_bars] ERROR: empty symbol")
        return None

    period = (period or "YEAR").upper().strip()
    if period not in VALID_PERIODS:
        print(f"[public_bars] ERROR: invalid period {period!r}")
        return None

    sym = symbol.upper().strip()

    if token is None:
        token = get_access_token()
        if token is None:
            return None

    url = PUBLIC_BARS_URL_TMPL.format(symbol=sym, period=period)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = _session.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        print(f"[public_bars] {sym} {period}: request failed: {e}")
        return None

    # One retry on 401 with a fresh token (the live stock bot does the same).
    if resp.status_code == 401:
        new_token = get_access_token(force_refresh=True)
        if new_token is None:
            return None
        try:
            resp = _session.get(
                url,
                headers={"Authorization": f"Bearer {new_token}"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            print(f"[public_bars] {sym} {period}: retry failed: {e}")
            return None

    if resp.status_code != 200:
        print(f"[public_bars] {sym} {period}: {_redacted_status(resp)}")
        return None

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError:
        print(f"[public_bars] {sym} {period}: non-JSON response")
        return None

    if not isinstance(payload, dict):
        return None

    raw_block = payload.get("regularMarket")
    raw_bars: List[Dict[str, Any]] = []
    if isinstance(raw_block, dict):
        rb = raw_block.get("bars")
        if isinstance(rb, list):
            raw_bars = [b for b in rb if isinstance(b, dict)]

    if not raw_bars:
        print(f"[public_bars] {sym} {period}: no bars (keys={list(payload.keys())})")
        return []

    normalized = [b for b in (_coerce_bar(b) for b in raw_bars) if b is not None]
    normalized.sort(key=lambda b: b.get("timestamp") or 0)
    return normalized


def latest_close(bars: List[Dict[str, Any]]) -> Optional[float]:
    """Return the close of the most recent bar, or None if bars is empty/missing."""
    if not bars:
        return None
    try:
        return float(bars[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return None


def sma(bars: List[Dict[str, Any]], period: int) -> Optional[float]:
    """Simple moving average of the most recent N closes. Returns None if
    fewer than N bars are available."""
    if not bars or period <= 0 or len(bars) < period:
        return None
    try:
        closes = [float(b["close"]) for b in bars[-period:]]
    except (KeyError, TypeError, ValueError):
        return None
    return sum(closes) / float(period)


__all__ = ["get_public_bars", "get_access_token", "latest_close", "sma"]
