# crypto_bot/data/market_data.py

from crypto_bot.exchange.public_api import get_crypto_prices


def get_price(symbol: str) -> float:
    """Returns latest spot price for a single symbol e.g. 'BTC'."""
    prices = get_crypto_prices([symbol])
    price = prices.get(symbol)
    # Audit L1: explicit None / non-positive check. The previous `if not price`
    # also treated 0.0 as "missing"; in practice crypto is never 0 but the
    # explicit guard removes that ambiguity.
    if price is None or price <= 0:
        raise ValueError(f"No price returned for {symbol}. Got: {prices}")
    return price