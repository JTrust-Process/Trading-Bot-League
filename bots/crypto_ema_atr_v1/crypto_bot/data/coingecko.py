# crypto_bot/data/coingecko.py
#
# Fetches OHLC candle data from CoinGecko's free API for ATR calculations.
# Free tier has rate limits (~10-30 req/min) — we cache results per cycle
# so we never call it more than once per symbol per run.

import requests
from typing import Any

# CoinGecko free API
OHLC_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"

# CoinGecko coin IDs for our symbols
COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
}

# In-process cache so a single bot run never hits the API twice for the same symbol
_cache: dict[str, list[list[float]]] = {}


def fetch_ohlc(symbol: str, days: int = 1) -> list[list[float]]:
    """
    Returns list of [timestamp_ms, open, high, low, close] candles.

    days=1  → 30-min candles (~48 candles)
    days=7  → 4-hour candles
    days=14 → daily candles

    For ATR(14) on 30-min timeframe, days=1 gives us 48 candles which is plenty.
    """
    cache_key = f"{symbol}_{days}"
    if cache_key in _cache:
        return _cache[cache_key]

    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return []

    url = OHLC_URL.format(coin_id=coin_id)
    try:
        resp = requests.get(
            url,
            params={"vs_currency": "usd", "days": days},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[coingecko] OHLC fetch for {symbol} returned {resp.status_code}")
            return []
        data: list[list[float]] = resp.json()
        if not isinstance(data, list):
            print(f"[coingecko] Unexpected OHLC response shape for {symbol}")
            return []
        _cache[cache_key] = data
        return data
    except Exception as e:
        print(f"[coingecko] OHLC fetch failed for {symbol}: {e}")
        return []


def compute_atr(symbol: str, period: int = 14) -> float | None:
    """
    Compute Average True Range from CoinGecko 30-min OHLC.

    True Range = max of:
      - high - low
      - abs(high - prev_close)
      - abs(low  - prev_close)

    Returns None if not enough data.
    """
    candles = fetch_ohlc(symbol, days=1)
    if len(candles) < period + 1:
        return None

    # Last `period+1` candles to compute `period` true ranges
    recent = candles[-(period + 1):]
    true_ranges: list[float] = []

    for i in range(1, len(recent)):
        _, _, high, low, _      = recent[i]
        _, _, _,    _,   pclose = recent[i - 1]
        tr = max(
            high - low,
            abs(high - pclose),
            abs(low  - pclose),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return None
    return sum(true_ranges) / len(true_ranges)