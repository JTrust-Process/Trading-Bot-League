"""league_core.public_api.equities — Public.com equity/ETF order client.

Two operations needed by ETF rotation (and any future equity-trading
League bot):

  place_market_buy(symbol, amount_usd)   — fractional dollar-notional BUY
  place_market_sell(symbol, quantity)    — share-quantity SELL

Plus a fill-price discovery helper:

  get_fill_price(order_id)               — poll /order/{id} for the fill

Wire format MATCHES exactly what the live stock bot
(`Trading Bot/Trading Bot Project/bot.py`) sends to Public today. We're
not inventing a new protocol — we're sharing the one your live bots have
already proven against production. Specifically:

  POST  /userapigateway/trading/{accountId}/order
  body  {
    "orderId":   "<deterministic uuid5>",
    "instrument": {"symbol": "SPY", "type": "EQUITY"},
    "orderSide": "BUY" | "SELL",
    "orderType": "MARKET",
    "expiration": {"timeInForce": "DAY"},
    "amount":    "250.00"      (BUY only — dollar notional, 2 decimals)
    "quantity":  "0.12345678"  (SELL only — shares, 8 decimals)
  }

Design rules:
  * NEVER raises. Every public function returns a structured dict the
    caller introspects with `result["ok"]`. Matches the soft-failure
    style used in league_core.status.
  * Deterministic order_id (uuid5 over account_id + minute + side + symbol)
    is the dedup mechanism: a retry inside the same minute hits the same
    order_id and Public rejects the duplicate. Cross-minute retries can
    in theory double-submit; ETF rotation's once-per-regime-change cadence
    makes that window negligible, but callers should still check for
    duplicate bot_trades rows before re-issuing.
  * Dry-run mode. Set `dry_run=True` (or env PUBLIC_DRY_RUN=1) to return
    the would-be payload without posting. Used by the smoke test and by
    any bot that wants to dress-rehearse before flipping to live.
  * 401 retry-once. If a stale cached token causes a 401, we force-refresh
    and retry ONE time. After that, surface the failure.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from league_core.public_api import auth


# ── Endpoints / constants ──────────────────────────────────────────────────

ORDER_URL_TMPL = "https://api.public.com/userapigateway/trading/{account_id}/order"

DEFAULT_TIMEOUT = 30.0
FILL_POLL_ATTEMPTS = 4
FILL_POLL_BACKOFF_SECONDS = 2.0

# Fields Public has historically populated for fill price (their API has
# shifted shapes over time — we tolerate every known spelling).
_FILL_PRICE_FIELDS = (
    "averagePrice", "avgFillPrice", "fillPrice", "averageFillPrice",
    "price", "filledPrice", "executedPrice", "avgPrice",
    "filled_price", "fill_price", "average_price",
)
_FILL_NESTED_KEYS = ("order", "fill", "execution", "orderExecution", "fills")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(f"[public_equities] {msg}", flush=True)


def _is_dry_run(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return bool(explicit)
    return (os.getenv("PUBLIC_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}


def deterministic_order_id(account_id: str, side: str, symbol: str) -> str:
    """Deterministic uuid5 keyed on (account, minute, side, symbol).

    Mirrors the existing stock bot's `deterministic_order_id`. Retrying
    the SAME (side, symbol) within the same minute produces the same UUID,
    so Public rejects the duplicate. Different minutes produce different
    UUIDs, which is why crash-recovery should consult bot_trades before
    re-issuing.

    Uses UTC minute granularity (the stock bot used NY tz; UTC works just
    as well for dedup and matches everything else in league_core)."""
    now_min = datetime.utcnow().strftime("%Y-%m-%d-%H-%M")
    seed = f"{account_id}:{now_min}:{side}:{symbol}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _build_payload(
    order_id: str,
    side: str,
    symbol: str,
    *,
    amount_usd: Optional[float] = None,
    quantity: Optional[float] = None,
) -> dict[str, Any]:
    """Construct the order body. Exactly one of amount_usd / quantity."""
    body: dict[str, Any] = {
        "orderId":    order_id,
        "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
        "orderSide":  side,
        "orderType":  "MARKET",
        "expiration": {"timeInForce": "DAY"},
    }
    if amount_usd is not None:
        body["amount"] = f"{round(float(amount_usd), 2):.2f}"
    if quantity is not None:
        body["quantity"] = f"{float(quantity):.8f}"
    return body


def _fail(order_id: Optional[str], error: str, *,
          status_code: Optional[int] = None,
          payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "ok":          False,
        "order_id":    order_id,
        "error":       error,
        "status_code": status_code,
        "payload":     payload,
        "response":    None,
    }


def _ok(order_id: str, payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok":          True,
        "order_id":    order_id,
        "error":       None,
        "status_code": 200,
        "payload":     payload,
        "response":    response,
    }


def _dry(order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok":          True,
        "order_id":    order_id,
        "error":       None,
        "status_code": None,
        "payload":     payload,
        "response":    {"dry_run": True},
    }


def _post_order(payload: dict[str, Any]) -> dict[str, Any]:
    """Common POST path used by both BUY and SELL. Returns the structured
    result dict. Handles a single 401-retry by refreshing the token.

    Pre-conditions: caller has constructed a payload with a valid order_id
    and has already enforced any risk gates (this function does NOT call
    risk.preflight — that's the caller's responsibility; see PLAN.md §4.3)."""
    if requests is None:
        return _fail(payload.get("orderId"), "requests_not_installed", payload=payload)

    account_id = auth.get_account_id()
    if not account_id:
        return _fail(payload.get("orderId"), "account_id_unresolved", payload=payload)

    url = ORDER_URL_TMPL.format(account_id=account_id)

    for attempt in (1, 2):
        headers = auth.auth_headers(force_refresh=(attempt == 2))
        if headers is None:
            return _fail(payload.get("orderId"), "auth_failed", payload=payload)
        headers = {**headers, "Content-Type": "application/json"}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            _print(f"POST order network error: {e!r}")
            return _fail(payload.get("orderId"), f"network_error: {e}", payload=payload)

        if resp.status_code == 401 and attempt == 1:
            _print("401 from Public — refreshing token and retrying once")
            continue

        if resp.status_code >= 400:
            _print(f"order rejected status={resp.status_code} body={resp.text[:300]!r}")
            return _fail(
                payload.get("orderId"),
                f"http_{resp.status_code}",
                status_code=resp.status_code,
                payload=payload,
            )

        try:
            body = resp.json()
        except ValueError:
            return _fail(payload.get("orderId"), "non_json_response",
                         status_code=resp.status_code, payload=payload)
        return _ok(payload["orderId"], payload, body)

    # Unreachable — the for-loop always returns.
    return _fail(payload.get("orderId"), "unknown_retry_exhaustion", payload=payload)


# ── Public API ──────────────────────────────────────────────────────────────

def place_market_buy(
    symbol: str,
    amount_usd: float,
    *,
    dry_run: Optional[bool] = None,
) -> dict[str, Any]:
    """Place a market BUY for `amount_usd` of `symbol` (fractional notional).

    Returns:
      {
        "ok":          bool,
        "order_id":    str | None,
        "error":       str | None,
        "status_code": int | None,
        "payload":     dict   (what we sent, or would have sent in dry-run),
        "response":    dict | None  (Public's response body on success),
      }

    Does NOT call risk.preflight — the caller MUST. Per PLAN.md §4.3 the
    risk gate is enforced at the bot level, just above this client, so
    other future asset classes (crypto, options) get the same gate without
    needing to know about it here.
    """
    if not symbol:
        return _fail(None, "symbol_empty")
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        return _fail(None, "amount_usd_invalid")
    if amount <= 0:
        return _fail(None, "amount_usd_non_positive")

    account_id = auth.get_account_id()
    if not account_id:
        return _fail(None, "account_id_unresolved")

    order_id = deterministic_order_id(account_id, "BUY", symbol.upper())
    payload = _build_payload(order_id, "BUY", symbol, amount_usd=amount)

    if _is_dry_run(dry_run):
        return _dry(order_id, payload)
    return _post_order(payload)


def place_market_sell(
    symbol: str,
    quantity: float,
    *,
    dry_run: Optional[bool] = None,
) -> dict[str, Any]:
    """Place a market SELL of `quantity` shares of `symbol`.

    Same result shape as `place_market_buy`. Same risk-gate caveat: caller
    must call risk.preflight first. Same dry-run behavior.
    """
    if not symbol:
        return _fail(None, "symbol_empty")
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return _fail(None, "quantity_invalid")
    if qty <= 0:
        return _fail(None, "quantity_non_positive")

    account_id = auth.get_account_id()
    if not account_id:
        return _fail(None, "account_id_unresolved")

    order_id = deterministic_order_id(account_id, "SELL", symbol.upper())
    payload = _build_payload(order_id, "SELL", symbol, quantity=qty)

    if _is_dry_run(dry_run):
        return _dry(order_id, payload)
    return _post_order(payload)


# ── Fill-price discovery ───────────────────────────────────────────────────

def get_fill_price(order_id: str) -> Optional[float]:
    """Poll /order/{order_id} for the average fill price. None if we can't
    determine it.

    Public's order GET response shape has shifted over time — we tolerate
    every known field name. Retries up to FILL_POLL_ATTEMPTS times with a
    FILL_POLL_BACKOFF_SECONDS delay between attempts (no delay on the
    first try, so an instantly-filled market order pays zero latency tax).

    Returns the fill price as float on success. None on failure (caller
    should mark the bot_trade entry price as estimated using the bar
    close, same fallback the stock bot uses).
    """
    if requests is None or not order_id:
        return None

    account_id = auth.get_account_id()
    if not account_id:
        return None

    url = f"{ORDER_URL_TMPL.format(account_id=account_id)}/{order_id}"
    last_keys: list[str] = []

    for attempt in range(FILL_POLL_ATTEMPTS):
        if attempt > 0:
            time.sleep(FILL_POLL_BACKOFF_SECONDS)
        headers = auth.auth_headers(force_refresh=(attempt == 1))
        if headers is None:
            return None
        try:
            resp = requests.get(url, headers=headers, timeout=15.0)
        except requests.RequestException as e:
            _print(f"fill poll attempt {attempt+1} network error: {e}")
            continue
        if resp.status_code != 200:
            _print(f"fill poll attempt {attempt+1} status={resp.status_code}")
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        if isinstance(data, dict):
            last_keys = list(data.keys())
            fp = _extract_fill_price(data)
            if fp is not None:
                return fp
            # If Public reports FILLED but we couldn't parse a price, no
            # point retrying — log loudly and bail.
            status = str(data.get("status") or data.get("orderStatus") or "").upper()
            if status == "FILLED":
                _print(f"order FILLED but no price field matched. "
                       f"order_id={order_id} keys={last_keys}")
                return None

    _print(f"could not determine fill price for order_id={order_id} "
           f"after {FILL_POLL_ATTEMPTS} attempts; last_keys={last_keys}")
    return None


def _extract_fill_price(data: dict[str, Any]) -> Optional[float]:
    """Scan a /order/{id} response dict for any known fill-price field.
    Top-level first, then a few common nested objects."""
    for field in _FILL_PRICE_FIELDS:
        val = data.get(field)
        if val is None:
            continue
        try:
            fp = float(str(val))
            if fp > 0:
                return fp
        except (TypeError, ValueError):
            pass
    for key in _FILL_NESTED_KEYS:
        nested = data.get(key)
        if not isinstance(nested, dict):
            continue
        for field in _FILL_PRICE_FIELDS:
            val = nested.get(field)
            if val is None:
                continue
            try:
                fp = float(str(val))
                if fp > 0:
                    return fp
            except (TypeError, ValueError):
                pass
    return None


__all__ = [
    "ORDER_URL_TMPL",
    "place_market_buy",
    "place_market_sell",
    "get_fill_price",
    "deterministic_order_id",
]
