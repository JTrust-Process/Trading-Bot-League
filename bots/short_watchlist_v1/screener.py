"""bots/short_watchlist_v1/screener.py — setup detection (pure functions).

Entry rules (ALL must be true for a SHORT setup):

  1. Close < SMA50                          — confirmed downtrend
  2. Close < SMA200                         — long-term downtrend
  3. Close <= recent 20-day low             — breakdown
  4. 3-month return <= -5%                  — negative momentum

Exit rules (ANY ONE triggers a COVER):

  A. Close > SMA20                          — trend reversal
  B. Adverse move >= ADVERSE_PCT from entry — stop (price above entry by this %)
  C. Favorable move >= FAVORABLE_PCT from entry — take-profit (price below entry)

All thresholds are module-level constants — tune freely; they're advisory.

Universe is liquid US equities + a couple of broad-market index ETFs.
ETFs are included so we can short the index as a hedge proxy when many
underlying names already broke down (a real risk manager would do this).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


# Curated, high-liquidity universe. Mirrors the stock bot's MOMENTUM_SYMBOLS
# plus a small-cap index proxy (IWM) and a tech sector ETF (XLK).
UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "QQQ", "SPY", "IWM", "XLK",
)


# ── Entry thresholds ────────────────────────────────────────────────────────
BREAKDOWN_LOOKBACK = 20            # days to compute the breakdown low
MOMENTUM_LOOKBACK  = 63            # ~3 months of trading days
MOMENTUM_MAX_PCT   = -0.05         # require 3-month return <= -5%

# ── Exit thresholds ─────────────────────────────────────────────────────────
TREND_SMA_PERIOD   = 20            # close > SMA20 = trend reversal
ADVERSE_PCT        = 0.05          # 5% adverse move = stop (price up vs entry)
FAVORABLE_PCT      = 0.10          # 10% favorable move = take-profit (price down)


# ── Helpers ─────────────────────────────────────────────────────────────────


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


def _rolling_low(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return min(values[-period:])


def _ret_n(values: List[float], n: int) -> Optional[float]:
    if len(values) < n + 1:
        return None
    start, end = values[-(n + 1)], values[-1]
    if start <= 0:
        return None
    return (end / start) - 1.0


# ── Entry detection ─────────────────────────────────────────────────────────


@dataclass
class EntrySignal:
    symbol: str
    close: float
    sma50: float
    sma200: float
    rolling_low: float
    ret_3m: float
    confidence: float           # 0..1 derived from rule-strength
    rationale: str


def detect_entry(symbol: str, bars: List[Dict]) -> Optional[EntrySignal]:
    """Return an EntrySignal if all entry rules pass, else None.

    Needs at least 200 bars of history for SMA200; with the YEAR period
    (~252 daily bars) this is normally fine, but defends against thin
    universes.
    """
    closes = _closes(bars)
    if len(closes) < max(200, MOMENTUM_LOOKBACK + 1, BREAKDOWN_LOOKBACK + 1):
        return None

    last = closes[-1]
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    rl = _rolling_low(closes, BREAKDOWN_LOOKBACK)
    ret_3m = _ret_n(closes, MOMENTUM_LOOKBACK)

    if None in (sma50, sma200, rl, ret_3m):
        return None
    # Rules: explicit floats so type-checkers don't complain.
    sma50_v: float = sma50            # type: ignore[assignment]
    sma200_v: float = sma200          # type: ignore[assignment]
    rl_v: float = rl                  # type: ignore[assignment]
    ret_3m_v: float = ret_3m          # type: ignore[assignment]

    if not (last < sma50_v and last < sma200_v):
        return None
    if not (last <= rl_v):
        return None
    if not (ret_3m_v <= MOMENTUM_MAX_PCT):
        return None

    # Confidence: how far below the SMAs we are, plus how negative the
    # 3m return is. Bounded into [0.5, 1.0] — every setup we publish is
    # already moderately strong by construction.
    dist_50  = max(0.0, (sma50_v - last) / sma50_v)         # how far under SMA50, %
    dist_200 = max(0.0, (sma200_v - last) / sma200_v)       # how far under SMA200, %
    mom_strength = min(1.0, abs(ret_3m_v) / 0.20)           # -20% caps the scale
    composite = 0.4 * min(1.0, dist_50 / 0.10) \
              + 0.3 * min(1.0, dist_200 / 0.10) \
              + 0.3 * mom_strength
    confidence = 0.5 + 0.5 * composite
    confidence = max(0.5, min(1.0, confidence))

    rationale = (
        f"close {last:.2f} < SMA50 {sma50_v:.2f} & SMA200 {sma200_v:.2f}; "
        f"at/below {BREAKDOWN_LOOKBACK}-day low {rl_v:.2f}; "
        f"3m return {ret_3m_v*100:+.2f}%"
    )

    return EntrySignal(
        symbol=symbol,
        close=last,
        sma50=sma50_v,
        sma200=sma200_v,
        rolling_low=rl_v,
        ret_3m=ret_3m_v,
        confidence=confidence,
        rationale=rationale,
    )


# ── Exit detection ──────────────────────────────────────────────────────────


@dataclass
class ExitSignal:
    symbol: str
    close: float
    sma20: Optional[float]
    reason: str                 # 'trend_reversal' | 'stop' | 'take_profit'
    rationale: str


def detect_exit(
    symbol: str,
    bars: List[Dict],
    entry_price: float,
) -> Optional[ExitSignal]:
    """Return an ExitSignal if any exit rule triggers, else None.

    entry_price is the simulated short's fill price (from bot_positions
    .entry_price). Caller fetched it from Supabase.
    """
    closes = _closes(bars)
    if not closes:
        return None
    last = closes[-1]
    sma20 = _sma(closes, TREND_SMA_PERIOD)

    # Rule A — trend reversal (close > SMA20)
    if sma20 is not None and last > sma20:
        return ExitSignal(
            symbol=symbol,
            close=last,
            sma20=sma20,
            reason="trend_reversal",
            rationale=f"close {last:.2f} > SMA20 {sma20:.2f}",
        )

    if entry_price <= 0:
        return None

    move_pct = (last - entry_price) / entry_price

    # Rule B — adverse move (price went UP — bad for a short)
    if move_pct >= ADVERSE_PCT:
        return ExitSignal(
            symbol=symbol,
            close=last,
            sma20=sma20,
            reason="stop",
            rationale=f"adverse move {move_pct*100:+.2f}% from entry {entry_price:.2f}",
        )

    # Rule C — favorable move (price fell ≥ FAVORABLE_PCT — good for a short)
    if move_pct <= -FAVORABLE_PCT:
        return ExitSignal(
            symbol=symbol,
            close=last,
            sma20=sma20,
            reason="take_profit",
            rationale=f"favorable move {move_pct*100:+.2f}% from entry {entry_price:.2f}",
        )

    return None


__all__ = [
    "UNIVERSE",
    "BREAKDOWN_LOOKBACK",
    "MOMENTUM_LOOKBACK",
    "MOMENTUM_MAX_PCT",
    "TREND_SMA_PERIOD",
    "ADVERSE_PCT",
    "FAVORABLE_PCT",
    "EntrySignal",
    "ExitSignal",
    "detect_entry",
    "detect_exit",
]
