# crypto_bot/exchange/public_api.py

import uuid
import requests
from crypto_bot.config.settings import get_public_api_key
from crypto_bot.utils.retry import retry

AUTH_URL        = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL     = "https://api.public.com/userapigateway/trading/account"
QUOTES_URL_TMPL = "https://api.public.com/userapigateway/marketdata/{accountId}/quotes"
ORDER_URL_TMPL  = "https://api.public.com/userapigateway/trading/{accountId}/order"

INSTRUMENT_TYPE = "CRYPTO"

_access_token: str | None = None


def _invalidate_token() -> None:
    """Clear the cached bearer so the next call re-authenticates."""
    global _access_token
    _access_token = None


def _raise_with_body(resp: requests.Response) -> None:
    # Audit H2: on 401 we drop the cached token before raising so the
    # retry decorator's next attempt mints a fresh one. Without this the
    # process would keep replaying a dead bearer until the run timed out.
    if resp.status_code == 401:
        _invalidate_token()
    if resp.status_code >= 400:
        raise RuntimeError(
            f"HTTP {resp.status_code} from {resp.url}\n"
            f"Response body: {resp.text[:500]}"
        )


# ── Auth ──────────────────────────────────────────────────────────────────────

@retry(max_attempts=3, delay=2)
def get_access_token(validity_minutes: int = 60) -> str:
    global _access_token
    if _access_token is not None:
        return _access_token
    resp = requests.post(
        AUTH_URL,
        headers={"Content-Type": "application/json"},
        json={"secret": get_public_api_key(), "validityInMinutes": validity_minutes},
        timeout=30,
    )
    _raise_with_body(resp)
    token = resp.json().get("accessToken")
    if not token:
        raise RuntimeError(f"No accessToken in response: {resp.text[:300]}")
    _access_token = token
    return _access_token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


# ── Account ───────────────────────────────────────────────────────────────────

@retry(max_attempts=3, delay=2)
def get_accounts() -> dict:
    resp = requests.get(ACCOUNT_URL, headers=_headers(), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def get_primary_account_id() -> str:
    data = get_accounts()
    accounts = data.get("accounts", [])
    if accounts:
        return accounts[0]["accountId"]
    raise RuntimeError(f"No accounts found. Full response: {data}")


# ── Market data ───────────────────────────────────────────────────────────────

@retry(max_attempts=3, delay=2)
def get_quotes(account_id: str, symbols: list[str]) -> list[dict]:
    url = QUOTES_URL_TMPL.format(accountId=account_id)
    body = {
        "instruments": [
            {"symbol": s, "type": INSTRUMENT_TYPE} for s in symbols
        ]
    }
    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    _raise_with_body(resp)
    return resp.json().get("quotes", [])


def get_crypto_prices(symbols: list[str]) -> dict[str, float]:
    acct_id = get_primary_account_id()
    quotes = get_quotes(acct_id, symbols)
    prices = {}
    for q in quotes:
        sym = q.get("instrument", {}).get("symbol")
        last = q.get("last")
        if sym and last:
            try:
                prices[sym] = float(last)
            except (ValueError, TypeError):
                pass
    return prices


# ── Order placement ───────────────────────────────────────────────────────────

@retry(max_attempts=3, delay=2)
def place_order_buy(
    account_id: str,
    symbol: str,
    amount_usd: float,
    order_type: str = "MARKET",
) -> dict:
    """
    BUY using notional USD amount — e.g. spend $10 of BTC.
    Public accepts `amount` for buys.
    """
    url = ORDER_URL_TMPL.format(accountId=account_id)
    order_id = str(uuid.uuid4())
    body = {
        "orderId": order_id,
        "instrument": {"symbol": symbol, "type": INSTRUMENT_TYPE},
        "orderSide": "BUY",
        "orderType": order_type.upper(),
        "expiration": {"timeInForce": "DAY"},
        "amount": str(round(amount_usd, 2)),
    }
    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    _raise_with_body(resp)
    result = resp.json()
    result["_clientOrderId"] = order_id
    return result


@retry(max_attempts=3, delay=2)
def place_order_sell(
    account_id: str,
    symbol: str,
    quantity: float,
    order_type: str = "MARKET",
) -> dict:
    """
    SELL using exact quantity — e.g. sell 0.000138 BTC.
    Public requires `quantity` for sells when closing a position.
    """
    url = ORDER_URL_TMPL.format(accountId=account_id)
    order_id = str(uuid.uuid4())
    body = {
        "orderId": order_id,
        "instrument": {"symbol": symbol, "type": INSTRUMENT_TYPE},
        "orderSide": "SELL",
        "orderType": order_type.upper(),
        "expiration": {"timeInForce": "DAY"},
        "quantity": str(round(quantity, 8)),
    }
    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    _raise_with_body(resp)
    result = resp.json()
    result["_clientOrderId"] = order_id
    return result


@retry(max_attempts=3, delay=2)
def get_order(account_id: str, order_id: str) -> dict:
    """Poll for order fill status after placing an order.

    Audit M2: verify against Public's current API docs that the path is
    `/order/{order_id}` and not `/orders/{order_id}` — both spellings appear
    in different broker APIs. If this returns 404 in your bot_errors table,
    that's where to look first. _poll_fill_price degrades gracefully (uses
    the quote price as fallback), so a wrong URL won't break trades, but
    you will lose averagePrice accuracy and post-fill quantity verification.
    """
    url = f"https://api.public.com/userapigateway/trading/{account_id}/order/{order_id}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    _raise_with_body(resp)
    return resp.json()


# ── Portfolio / buying power ───────────────────────────────────────────────────

@retry(max_attempts=3, delay=2)
def get_portfolio(account_id: str) -> dict:
    """
    GET /userapigateway/trading/{accountId}/portfolio/v2
    Returns full portfolio including buyingPower and positions.
    """
    url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
    resp = requests.get(url, headers=_headers(), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def get_cash_buying_power(account_id: str) -> float:
    """
    Returns available cash buying power in USD.
    Uses cashOnlyBuyingPower to avoid trading on margin.
    Falls back to 0.0 if unavailable.
    """
    try:
        portfolio = get_portfolio(account_id)
        bp = portfolio.get("buyingPower", {})
        for key in ("cashOnlyBuyingPower", "buyingPower", "availableBuyingPower"):
            val = bp.get(key)
            if val is not None:
                return float(val)
    except Exception as e:
        print(f"[public_api] get_cash_buying_power failed: {e}")
    return 0.0


def get_crypto_position_quantity(account_id: str, symbol: str) -> float | None:
    """
    Returns the actual quantity of a crypto position held in the account.
    Uses portfolio/v2 to get the real filled quantity from Public.
    Returns None if position not found or on error.

    This is used before selling to ensure we never try to sell more
    than we actually hold, avoiding "would result in short position" errors.
    """
    try:
        portfolio = get_portfolio(account_id)
        for pos in (portfolio.get("positions") or []):
            inst = pos.get("instrument") or {}
            if (inst.get("type") == "CRYPTO" and
                    inst.get("symbol", "").upper() == symbol.upper()):
                qty = pos.get("quantity")
                if qty is not None:
                    return float(qty)
    except Exception as e:
        print(f"[public_api] get_crypto_position_quantity failed: {e}")
    return None