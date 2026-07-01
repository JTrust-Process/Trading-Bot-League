# crypto_bot/strategy/signal.py
#
# EMA 9/21 crossover + 50-EMA regime filter + ATR-based volatility scaling.
#
# Strategy improvements (audit part 2):
#   - Regime filter: only allow BUY when price > EMA50 AND EMA50 trending up
#   - This filters out crossovers that happen during ranging/bearish periods
#     where we'd just get chopped up
#
# The regime filter is APPLIED IN engine.py (not here) so HOLD signals
# during chop are still logged with helpful context.

from crypto_bot.config.settings import EMA_FAST, EMA_SLOW

REGIME_EMA_PERIOD = 50  # higher-timeframe-ish trend filter
REGIME_LOOKBACK   = 5   # how many candles back to check if EMA50 is rising


def ema(values: list, period: int) -> list:
    """EMA with SMA seeding — converges faster than seeding from values[0]."""
    if len(values) < period:
        # Not enough data — running average
        result = []
        running = 0.0
        for i, v in enumerate(values):
            running += v
            result.append(running / (i + 1))
        return result

    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result = [seed] * period
    for price in values[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def generate_signal(prices: list, in_position: bool) -> tuple[str, float]:
    """
    Returns (signal, gap_pct).
    Pure crossover detection — regime filtering happens in engine.py.
    """
    if len(prices) < EMA_SLOW + 2:
        return "HOLD", 0.0

    ema_fast = ema(prices, EMA_FAST)
    ema_slow = ema(prices, EMA_SLOW)

    fast_now,  slow_now  = ema_fast[-1],  ema_slow[-1]
    fast_prev, slow_prev = ema_fast[-2],  ema_slow[-2]

    crossed_up   = fast_prev <  slow_prev and fast_now > slow_now
    crossed_down = fast_prev >  slow_prev and fast_now < slow_now

    current_price = prices[-1]
    gap_pct = abs(fast_now - slow_now) / current_price if current_price else 0.0

    if not in_position and crossed_up:
        return "BUY", gap_pct
    if in_position and crossed_down:
        return "SELL", gap_pct

    return "HOLD", gap_pct


# ── Regime detection (NEW) ────────────────────────────────────────────────────

def regime_check(prices: list) -> tuple[bool, str]:
    """
    Returns (is_uptrend, reason).

    is_uptrend = True  → market is trending up; BUY signals allowed
    is_uptrend = False → market is ranging/bearish OR warming up; HOLD all BUY signals

    Conditions for uptrend:
      1. Current price > EMA50  (price is above the trend line)
      2. EMA50 is RISING  (compare current vs N candles ago)

    During warmup (< 55 prices), we BLOCK buys rather than allow them.
    Audit found this to be a real safety issue: if state.json ever resets
    mid-deployment, the bot would happily buy with no trend filter active.
    Blocking during warmup means safer first hours after any state reset,
    at the cost of ~14 hours of "no trades" after first deploy.
    """
    if len(prices) < REGIME_EMA_PERIOD + REGIME_LOOKBACK:
        return False, f"warming up regime filter ({len(prices)}/{REGIME_EMA_PERIOD + REGIME_LOOKBACK} prices)"

    ema50      = ema(prices, REGIME_EMA_PERIOD)
    current    = prices[-1]
    ema_now    = ema50[-1]
    ema_before = ema50[-1 - REGIME_LOOKBACK]

    price_above_ema = current > ema_now
    ema_rising      = ema_now > ema_before

    if price_above_ema and ema_rising:
        slope_pct = ((ema_now / ema_before) - 1) * 100
        return True, f"uptrend (price ${current:.2f} > EMA50 ${ema_now:.2f}, slope +{slope_pct:.3f}% over {REGIME_LOOKBACK} candles)"

    reasons = []
    if not price_above_ema:
        reasons.append(f"price ${current:.2f} below EMA50 ${ema_now:.2f}")
    if not ema_rising:
        slope_pct = ((ema_now / ema_before) - 1) * 100
        reasons.append(f"EMA50 flat/falling ({slope_pct:+.3f}% over {REGIME_LOOKBACK} candles)")
    return False, "; ".join(reasons)


def warmup_progress(prices: list) -> str:
    needed = EMA_SLOW + 2
    return f"{len(prices)}/{needed} prices collected"