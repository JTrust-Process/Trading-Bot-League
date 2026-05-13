"""bots/options_alert_v1/screener.py — strategy suggestion logic (pure).

Per underlying, we compute two signals:

  Volatility regime — annualized realized vol over the last VOL_WINDOW
  days, ratioed against the 1-year baseline. Three buckets:
     low_vol  : ratio < 0.70   (favor selling premium — collect decay)
     mid_vol  : 0.70 ≤ r < 1.50 (favor defined-risk spreads)
     high_vol : ratio ≥ 1.50    (favor buying premium — large moves)

  Trend regime — close vs SMA50 / SMA200, same as the rest of the platform.
     bull   : close > SMA50 AND close > SMA200
     bear   : close < SMA50 AND close < SMA200
     mixed  : everything else

Cross those two and you get a 3 × 3 strategy matrix. Map each cell to
a single recommended strategy family. The output is one Idea per
underlying per cycle — no specific strikes, no expirations, no Greeks.
That's intentional for v1.

Purely defined-risk strategies are preferred even when leverage is
higher — we never suggest naked calls or naked puts.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict, List, Optional


# Universe — liquid optionable underlyings.
UNIVERSE = (
    "SPY", "QQQ", "IWM",         # broad / sector ETFs
    "AAPL", "NVDA", "TSLA",      # high-volume single-name options
)


# Vol buckets
VOL_WINDOW             = 21        # ~1 month of trading days
VOL_BASELINE_WINDOW    = 252       # ~1 year
VOL_LOW_RATIO          = 0.70
VOL_HIGH_RATIO         = 1.50

# Trend SMA periods (match the rest of the platform)
TREND_FAST_PERIOD      = 50
TREND_SLOW_PERIOD      = 200


# ── Helpers (pure) ──────────────────────────────────────────────────────────


def _closes(bars: List[Dict]) -> List[float]:
    out: List[float] = []
    for b in bars:
        try:
            out.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _sma(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def _annualized_vol(values: List[float], lookback: int) -> Optional[float]:
    if len(values) < lookback + 1:
        return None
    series = values[-(lookback + 1):]
    rets: List[float] = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1], series[i]
        if prev <= 0 or cur <= 0:
            continue
        rets.append((cur - prev) / prev)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return sqrt(var) * sqrt(252.0)


# ── Regime + idea ───────────────────────────────────────────────────────────


@dataclass
class Idea:
    symbol: str
    close: float
    trend: str                 # 'bull' | 'bear' | 'mixed'
    vol_bucket: str            # 'low_vol' | 'mid_vol' | 'high_vol'
    realized_vol: float        # annualized
    baseline_vol: float        # annualized over baseline window
    vol_ratio: float
    strategy: str              # short, descriptive name
    rationale: str             # human-readable explanation
    confidence: float          # 0..1
    metrics: Dict[str, Optional[float]]


# 3 × 3 strategy mapping. Defined-risk where possible; never naked.
_STRATEGY_MATRIX: Dict[tuple[str, str], tuple[str, str]] = {
    # (trend, vol_bucket) -> (strategy_name, rationale_template)
    ("bull",  "low_vol"):  (
        "covered_call",
        "Bullish + cheap premium → sell OTM calls against shares to harvest "
        "decay. Defined risk if you already own the underlying.",
    ),
    ("bull",  "mid_vol"):  (
        "bull_put_spread",
        "Bullish + neutral premium → sell put spread below the market. "
        "Defined max loss; capped reward.",
    ),
    ("bull",  "high_vol"): (
        "long_call_spread",
        "Bullish + rich premium → buy a debit call spread. Pay reduced net "
        "premium thanks to the short call leg, defined max loss.",
    ),
    ("bear",  "low_vol"):  (
        "iron_condor_skewed_down",
        "Bearish + cheap premium → asymmetric iron condor with the short "
        "call closer to current. Defined risk both sides.",
    ),
    ("bear",  "mid_vol"):  (
        "bear_call_spread",
        "Bearish + neutral premium → sell call spread above the market. "
        "Defined max loss; capped reward.",
    ),
    ("bear",  "high_vol"): (
        "long_put_spread",
        "Bearish + rich premium → buy a debit put spread. Reduced net cost "
        "from the short put leg, defined max loss.",
    ),
    ("mixed", "low_vol"):  (
        "iron_condor",
        "No clear trend + cheap premium → neutral iron condor for range-"
        "bound conditions. Defined risk both sides.",
    ),
    ("mixed", "mid_vol"):  (
        "calendar_spread",
        "No clear trend + neutral premium → calendar (sell front-month, buy "
        "back-month) to harvest near-term decay. Defined risk.",
    ),
    ("mixed", "high_vol"): (
        "long_strangle",
        "No clear trend + rich premium → long strangle to capture a "
        "directional break either way. Defined max loss = total premium.",
    ),
}


def _classify_vol(realized: float, baseline: float) -> tuple[str, float]:
    if baseline <= 0:
        return "mid_vol", 1.0
    ratio = realized / baseline
    if ratio < VOL_LOW_RATIO:
        return "low_vol", ratio
    if ratio >= VOL_HIGH_RATIO:
        return "high_vol", ratio
    return "mid_vol", ratio


def _classify_trend(close: float, sma50: float, sma200: float) -> str:
    if close > sma50 and close > sma200:
        return "bull"
    if close < sma50 and close < sma200:
        return "bear"
    return "mixed"


def derive_idea(symbol: str, bars: List[Dict]) -> Optional[Idea]:
    """Return one Idea for the symbol, or None if data is insufficient."""
    closes = _closes(bars)
    if len(closes) < TREND_SLOW_PERIOD + 1 or len(closes) < VOL_BASELINE_WINDOW + 1:
        return None

    last = closes[-1]
    sma50 = _sma(closes, TREND_FAST_PERIOD)
    sma200 = _sma(closes, TREND_SLOW_PERIOD)
    if sma50 is None or sma200 is None:
        return None

    realized = _annualized_vol(closes, VOL_WINDOW)
    baseline = _annualized_vol(closes, VOL_BASELINE_WINDOW)
    if realized is None or baseline is None or baseline <= 0:
        return None

    vol_bucket, vol_ratio = _classify_vol(realized, baseline)
    trend = _classify_trend(last, sma50, sma200)
    strategy, rationale_template = _STRATEGY_MATRIX[(trend, vol_bucket)]

    # Confidence: distance from cluster boundaries. Trends that are deep in
    # one direction and vol that's far from the bucket cuts get higher
    # confidence than borderline cases.
    trend_dist_50  = abs(last - sma50) / sma50
    trend_dist_200 = abs(last - sma200) / sma200
    trend_strength = min(1.0, (trend_dist_50 + trend_dist_200) / 0.20)

    if vol_bucket == "low_vol":
        vol_strength = min(1.0, (VOL_LOW_RATIO - vol_ratio) / VOL_LOW_RATIO)
    elif vol_bucket == "high_vol":
        vol_strength = min(1.0, (vol_ratio - VOL_HIGH_RATIO) / VOL_HIGH_RATIO)
    else:
        # mid_vol: confidence is "how centered between boundaries"
        center = (VOL_LOW_RATIO + VOL_HIGH_RATIO) / 2.0
        half_range = (VOL_HIGH_RATIO - VOL_LOW_RATIO) / 2.0
        vol_strength = max(0.0, 1.0 - abs(vol_ratio - center) / half_range)

    confidence = max(0.4, min(1.0, 0.4 + 0.3 * trend_strength + 0.3 * vol_strength))

    rationale = (
        f"close {last:.2f} (SMA50 {sma50:.2f} / SMA200 {sma200:.2f}); "
        f"realized vol {realized*100:.1f}% vs baseline {baseline*100:.1f}% "
        f"(ratio {vol_ratio:.2f}); regime={trend}/{vol_bucket}. "
        f"{rationale_template}"
    )

    return Idea(
        symbol=symbol,
        close=last,
        trend=trend,
        vol_bucket=vol_bucket,
        realized_vol=realized,
        baseline_vol=baseline,
        vol_ratio=vol_ratio,
        strategy=strategy,
        rationale=rationale,
        confidence=confidence,
        metrics={
            "close":         last,
            "sma50":         sma50,
            "sma200":        sma200,
            "realized_vol":  realized,
            "baseline_vol":  baseline,
            "vol_ratio":     vol_ratio,
            "trend_strength": trend_strength,
            "vol_strength":  vol_strength,
        },
    )


__all__ = [
    "UNIVERSE",
    "VOL_WINDOW",
    "VOL_BASELINE_WINDOW",
    "VOL_LOW_RATIO",
    "VOL_HIGH_RATIO",
    "TREND_FAST_PERIOD",
    "TREND_SLOW_PERIOD",
    "Idea",
    "derive_idea",
]
