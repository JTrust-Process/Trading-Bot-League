"""bots/etf_rotation_v1/strategy.py — regime detection and target allocation.

Strategy v1 (deliberately tiny):

  Regime:    SPY close > SPY 50-day SMA   →  'bull'
             otherwise                     →  'bear'

  Target allocation:
    bull  →  SPY 25%, QQQ 25%, VTI 25%, SCHD 25%, SGOV 0%
    bear  →  100% SGOV (cash-like)

  Rebalance trigger: any change in target set vs. last known target.
  No partial rebalances; on regime change we close everything and open
  the new positions at the latest close. That's it.

This file does no I/O. Inputs are bars (list-of-dicts from public_bars);
output is a Plan dataclass the main loop consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from league_core.public_bars import latest_close, sma


# Universe — must be a subset of the bot's allowed_instruments in bot_registry.
RISK_ON   = ("SPY", "QQQ", "VTI", "SCHD")
RISK_OFF  = ("SGOV",)
UNIVERSE  = RISK_ON + RISK_OFF

# Regime SMA window. Matches the stock bot's REGIME_SLOW default (50 days).
REGIME_SMA_PERIOD = 50


def _equal_weight(symbols: tuple[str, ...]) -> Dict[str, float]:
    if not symbols:
        return {}
    w = 1.0 / len(symbols)
    return {s: w for s in symbols}


def target_for_regime(regime: str) -> Dict[str, float]:
    """Map regime label to target weights."""
    if regime == "bull":
        return _equal_weight(RISK_ON)
    return _equal_weight(RISK_OFF)


@dataclass
class Plan:
    regime: str                 # 'bull' | 'bear' | 'unknown'
    regime_reason: str
    spy_close: Optional[float]
    spy_sma: Optional[float]
    target_weights: Dict[str, float]   # {symbol: weight}


def derive_plan(spy_bars: list[dict]) -> Plan:
    """Compute the regime and resulting target allocation from SPY bars.

    spy_bars is the output of league_core.public_bars.get_public_bars("SPY", ...).
    """
    close = latest_close(spy_bars)
    spy_sma = sma(spy_bars, REGIME_SMA_PERIOD)

    if close is None or spy_sma is None:
        return Plan(
            regime="unknown",
            regime_reason="Insufficient SPY bars to compute regime.",
            spy_close=close,
            spy_sma=spy_sma,
            target_weights={},
        )

    if close > spy_sma:
        regime = "bull"
        reason = f"SPY close {close:.2f} > SMA{REGIME_SMA_PERIOD} {spy_sma:.2f}"
    else:
        regime = "bear"
        reason = f"SPY close {close:.2f} <= SMA{REGIME_SMA_PERIOD} {spy_sma:.2f}"

    return Plan(
        regime=regime,
        regime_reason=reason,
        spy_close=close,
        spy_sma=spy_sma,
        target_weights=target_for_regime(regime),
    )


__all__ = [
    "UNIVERSE",
    "RISK_ON",
    "RISK_OFF",
    "REGIME_SMA_PERIOD",
    "Plan",
    "derive_plan",
    "target_for_regime",
]
