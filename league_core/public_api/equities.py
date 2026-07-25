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

Pre-trade safety (added 2026-07-24)
----------------------------------
Before placing a real order we optionally run Public's preflight
endpoint and refuse the order if it would consume margin or fail
validation:

  POST /userapigateway/trading/{accountId}/preflight/single-leg

Same body as an order MINUS `orderId`. Response carries
`estimatedCost`, `buyingPowerRequirement`, `marginImpact`,
`marginRequirement`, `shortSelling`, fee estimates. Public's own docs
say "Always run preflight checks" and "Limits are enforced at order
placement — use the preflight endpoint to validate an order before
submitting."

NOTE ON `useMargin`: Public's changelog (2026-06-16) announced an
optional `useMargin` order field for forcing cash-only buying power.
As of 2026-07-24 that field does NOT appear in the documented request
schema for either place-order or preflight, nor in the official
"Placing your first equity order" guide. We therefore do NOT send it —
putting an unverified field into a live-capital order payload is the
wrong risk for a safety feature. Instead we enforce cash-only
OURSELVES from the preflight response (see `_evaluate_preflight`),
which is strictly stronger: it is documented behavior, and it also
surfaces cost/fee estimates. If Public later documents `useMargin`,
adding it is a one-line change in `_build_payload`.

Env flags:
  EQUITIES_PREFLIGHT         'true' (default) — run preflight before orders.
  EQUITIES_CASH_ONLY         'true' (default) — refuse orders that would
                             consume margin, per the preflight response.
  EQUITIES_PREFLIGHT_STRICT  'false' (default) — when true, ALSO refuse if
                             the preflight call itself fails (network/5xx).
                             Default false so a transient blip doesn't halt
                             trading; Public still enforces buying power at
                             placement, and league_core.risk already gated
                             this order upstream.

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
PREFLIGHT_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/preflight/single-leg"
)
PORTFOLIO_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
)

DEFAULT_TIMEOUT = 30.0
PREFLIGHT_TIMEOUT = 15.0
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


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var at call time. Empty/unset -> default."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _to_float(v: Any) -> Optional[float]:
    """Public returns numerics as JSON strings ("150.25"). Coerce, or None."""
    if v is None:
        return None
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


# ── Pre-trade safety: reason codes ─────────────────────────────────────────
# Short, stable strings — greppable in logs, usable as dashboard filters.
# Mirrors the convention in league_core.risk.

PRE_OK                  = "ok"
PRE_MARGIN_REQUIRED     = "preflight_margin_required"
PRE_INSUFFICIENT_CASH   = "preflight_insufficient_cash_buying_power"
PRE_VALIDATION_FAILED   = "preflight_validation_failed"
PRE_CALL_FAILED         = "preflight_call_failed"
PRE_NOT_SHORTABLE       = "preflight_not_shortable"


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


# ── Pre-trade safety: preflight + cash-only enforcement ────────────────────


def _evaluate_preflight(
    pf: dict[str, Any],
    *,
    side: str,
    cash_only: bool,
    cash_buying_power: Optional[float] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """PURE function. Given a preflight response body, decide whether to
    allow the order. Returns (allowed, reason_code, details).

    No IO, no env reads — every input is explicit so this is fully
    unit-testable. See league_core/_equities_smoke.py.

    Checks, in order:
      1. shortSelling.availability == 'NOT_SHORTABLE' on a SELL-to-open.
         (Only meaningful once a short bot exists; harmless for longs
         because Public omits the block for ordinary sells.)
      2. cash_only AND the response indicates margin would be consumed
         (marginImpact.marginUsageImpact > 0 OR
          marginImpact.initialMarginRequirement > 0 OR
          marginRequirement.longInitialRequirement > 0).
      3. cash_only AND cash_buying_power is known AND
         buyingPowerRequirement > cash_buying_power.

    `details` always carries the parsed estimates so the caller can log
    them regardless of the decision.
    """
    margin_impact = pf.get("marginImpact") or {}
    margin_req    = pf.get("marginRequirement") or {}
    short_block   = pf.get("shortSelling") or {}

    details: dict[str, Any] = {
        "order_value":              _to_float(pf.get("orderValue")),
        "estimated_cost":           _to_float(pf.get("estimatedCost")),
        "estimated_quantity":       _to_float(pf.get("estimatedQuantity")),
        "estimated_proceeds":       _to_float(pf.get("estimatedProceeds")),
        "buying_power_requirement": _to_float(pf.get("buyingPowerRequirement")),
        "estimated_commission":     _to_float(pf.get("estimatedCommission")),
        "margin_usage_impact":      _to_float(margin_impact.get("marginUsageImpact")),
        "initial_margin_req":       _to_float(margin_impact.get("initialMarginRequirement")),
        "long_initial_req":         _to_float(margin_req.get("longInitialRequirement")),
        "short_availability":       short_block.get("availability"),
        "cash_buying_power":        cash_buying_power,
    }

    # 1. Shortability — only blocks when Public explicitly says NOT_SHORTABLE.
    if (side or "").upper() == "SELL" and details["short_availability"] == "NOT_SHORTABLE":
        return (False, PRE_NOT_SHORTABLE, details)

    if not cash_only:
        return (True, PRE_OK, details)

    # 2. Any positive margin signal means this order would lean on margin.
    for key in ("margin_usage_impact", "initial_margin_req", "long_initial_req"):
        val = details[key]
        if val is not None and val > 0:
            return (False, PRE_MARGIN_REQUIRED, details)

    # 3. Requirement exceeds cash-only buying power.
    bpr = details["buying_power_requirement"]
    if (
        bpr is not None
        and cash_buying_power is not None
        and bpr > cash_buying_power
    ):
        return (False, PRE_INSUFFICIENT_CASH, details)

    return (True, PRE_OK, details)


def get_cash_only_buying_power() -> Optional[float]:
    """Fetch `buyingPower.cashOnlyBuyingPower` from portfolio v2.
    None on any failure (caller treats as 'unknown', not 'zero')."""
    if requests is None:
        return None
    account_id = auth.get_account_id()
    if not account_id:
        return None
    headers = auth.auth_headers()
    if headers is None:
        return None
    try:
        resp = requests.get(
            PORTFOLIO_URL_TMPL.format(account_id=account_id),
            headers=headers,
            timeout=PREFLIGHT_TIMEOUT,
        )
    except requests.RequestException as e:
        _print(f"portfolio fetch failed: {e!r}")
        return None
    if resp.status_code != 200:
        _print(f"portfolio fetch status={resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    bp = (data or {}).get("buyingPower") or {}
    return _to_float(bp.get("cashOnlyBuyingPower"))


def preflight_single_leg(
    symbol: str,
    side: str,
    *,
    amount_usd: Optional[float] = None,
    quantity: Optional[float] = None,
) -> dict[str, Any]:
    """Call Public's preflight endpoint for a single-leg MARKET order.

    Returns the same structured shape the order functions use:
      {ok, error, status_code, payload, response}

    `ok=False` with error='http_400' means Public rejected the order as
    invalid — that IS a definitive negative and callers should refuse.
    `ok=False` with a network error means we couldn't check.
    """
    if requests is None:
        return _fail(None, "requests_not_installed")
    account_id = auth.get_account_id()
    if not account_id:
        return _fail(None, "account_id_unresolved")

    # Same body as an order, minus orderId. validateOrder defaults true.
    payload: dict[str, Any] = {
        "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
        "orderSide":  side,
        "orderType":  "MARKET",
        "expiration": {"timeInForce": "DAY"},
    }
    if amount_usd is not None:
        payload["amount"] = f"{round(float(amount_usd), 2):.2f}"
    if quantity is not None:
        payload["quantity"] = f"{float(quantity):.8f}"

    url = PREFLIGHT_URL_TMPL.format(account_id=account_id)
    for attempt in (1, 2):
        headers = auth.auth_headers(force_refresh=(attempt == 2))
        if headers is None:
            return _fail(None, "auth_failed", payload=payload)
        headers = {**headers, "Content-Type": "application/json"}
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=PREFLIGHT_TIMEOUT)
        except requests.RequestException as e:
            _print(f"preflight network error: {e!r}")
            return _fail(None, f"network_error: {e}", payload=payload)

        if resp.status_code == 401 and attempt == 1:
            continue
        if resp.status_code >= 400:
            _print(f"preflight rejected status={resp.status_code} "
                   f"body={resp.text[:300]!r}")
            return _fail(None, f"http_{resp.status_code}",
                         status_code=resp.status_code, payload=payload)
        try:
            body = resp.json()
        except ValueError:
            return _fail(None, "non_json_response",
                         status_code=resp.status_code, payload=payload)
        return {
            "ok": True, "order_id": None, "error": None,
            "status_code": resp.status_code, "payload": payload,
            "response": body,
        }
    return _fail(None, "unknown_retry_exhaustion", payload=payload)


def _run_pre_trade_checks(
    symbol: str,
    side: str,
    *,
    amount_usd: Optional[float] = None,
    quantity: Optional[float] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Orchestrate the pre-trade safety checks. Returns
    (allowed, reason_code, details).

    Skipped entirely (allow) when EQUITIES_PREFLIGHT is off.
    On preflight CALL failure, honours EQUITIES_PREFLIGHT_STRICT:
      strict=False (default) -> allow, log a warning
      strict=True            -> refuse
    A 4xx from preflight is a DEFINITIVE validation failure and always
    refuses regardless of strict mode.
    """
    if not _env_flag("EQUITIES_PREFLIGHT", True):
        return (True, PRE_OK, {"skipped": "preflight_disabled"})

    cash_only = _env_flag("EQUITIES_CASH_ONLY", True)
    strict    = _env_flag("EQUITIES_PREFLIGHT_STRICT", False)

    pf = preflight_single_leg(symbol, side,
                              amount_usd=amount_usd, quantity=quantity)
    if not pf["ok"]:
        err = str(pf.get("error") or "")
        # A 4xx means Public evaluated the order and said no. Always refuse.
        if err.startswith("http_4"):
            return (False, PRE_VALIDATION_FAILED,
                    {"preflight_error": err,
                     "status_code": pf.get("status_code")})
        # Otherwise we simply couldn't check.
        if strict:
            return (False, PRE_CALL_FAILED, {"preflight_error": err})
        _print(f"preflight unavailable ({err}); proceeding "
               f"(EQUITIES_PREFLIGHT_STRICT=false)")
        return (True, PRE_OK, {"preflight_unavailable": err})

    cash_bp = get_cash_only_buying_power() if cash_only else None
    return _evaluate_preflight(
        pf["response"] or {},
        side=side,
        cash_only=cash_only,
        cash_buying_power=cash_bp,
    )


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

    # Dry-run short-circuits BEFORE preflight: no network, no account
    # state needed. Keeps the offline smoke tests offline.
    if _is_dry_run(dry_run):
        return _dry(order_id, payload)

    allowed, reason, details = _run_pre_trade_checks(
        symbol, "BUY", amount_usd=amount)
    if not allowed:
        _print(f"BUY {symbol} blocked by pre-trade check: {reason} {details}")
        result = _fail(order_id, reason, payload=payload)
        result["pre_trade"] = details
        return result

    result = _post_order(payload)
    result["pre_trade"] = details
    return result


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

    # Dry-run short-circuits BEFORE preflight — see place_market_buy.
    if _is_dry_run(dry_run):
        return _dry(order_id, payload)

    allowed, reason, details = _run_pre_trade_checks(
        symbol, "SELL", quantity=qty)
    if not allowed:
        _print(f"SELL {symbol} blocked by pre-trade check: {reason} {details}")
        result = _fail(order_id, reason, payload=payload)
        result["pre_trade"] = details
        return result

    result = _post_order(payload)
    result["pre_trade"] = details
    return result


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
    # Endpoints
    "ORDER_URL_TMPL",
    "PREFLIGHT_URL_TMPL",
    "PORTFOLIO_URL_TMPL",
    # Orders
    "place_market_buy",
    "place_market_sell",
    "get_fill_price",
    "deterministic_order_id",
    # Pre-trade safety
    "preflight_single_leg",
    "get_cash_only_buying_power",
    # Reason codes (stable, greppable)
    "PRE_OK",
    "PRE_MARGIN_REQUIRED",
    "PRE_INSUFFICIENT_CASH",
    "PRE_VALIDATION_FAILED",
    "PRE_CALL_FAILED",
    "PRE_NOT_SHORTABLE",
]
