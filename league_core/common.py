"""league_core.common — shared helpers that were copy-pasted across bots.

Created 2026-07-25 during a duplication audit. Every function here existed
in two or more bots with subtly different behavior. That divergence was not
theoretical:

  * `_classify` (symbol -> asset_class) had THREE different ETF allowlists.
    short_watchlist_v1 knew {QQQ, SPY, IWM, XLK}; options_alert_v1 knew
    {SPY, QQQ, IWM}; stock_momentum_v1 knew 30+. So XLK was recorded as
    'etf' when one bot logged it and 'equity' when another did, and any
    query grouping bot_trades by asset_class was wrong depending on which
    bot happened to write the row.

  * `_env_bool` existed as `_env_bool` (short_watchlist), `_env_flag`
    (league_core.public_api.equities) and `_pre_env_flag`
    (stock_momentum_v1.bot) — three names, same job.

  * `_env_float`, `_closes` and `_sma` were duplicated 3x each with
    identical bodies.

The rule going forward: if a helper is needed by a second bot, it moves
here rather than being copied. A fix then lands once instead of in five
places and missing the sixth.

NOTE ON THE VENDORED BOTS: stock_momentum_v1 and crypto_ema_atr_v1 were
vendored in from their own repos and keep their own local helpers on
purpose — they have their own conventions and a much larger blast radius.
They are NOT migrated to this module. The one exception worth making is
asset-class classification, because that writes to a SHARED table and its
divergence corrupts cross-bot queries.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  Environment parsing
# ─────────────────────────────────────────────────────────────────────────────
#
# All read at CALL time, never at import time. The bots call load_dotenv()
# inside main(), and a module-level os.getenv() would capture a stale value
# before that ran. Same rule as league_core.status.

_TRUTHY = {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    """Float from env, or `default` if unset/blank/unparseable."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    """Int from env, or `default` if unset/blank/unparseable.

    Tolerates values written as floats ("5.0" -> 5), which is a real case:
    env vars get edited by hand and "10.0" should not silently fall back to
    the default.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    """Bool from env. Truthy: 1/true/yes/on (case-insensitive).

    Anything else that is non-empty is FALSE — so a typo like "ture" reads
    as false rather than accidentally enabling something. For safety flags
    that default to on, that means a typo disables the guard; prefer
    checking the startup log over trusting the spelling.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def env_list(name: str, default: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """Comma-separated env var -> tuple of upper-cased, stripped entries.

    Used for symbol universes (SYMBOLS, SHORT_SYMBOLS, OPTIONS_SYMBOLS...).
    Empty/unset returns `default` as a tuple, or () when default is None.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return tuple(default or ())
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


# ─────────────────────────────────────────────────────────────────────────────
#  Asset-class classification
# ─────────────────────────────────────────────────────────────────────────────
#
# CANONICAL list. bot_trades.asset_class and bot_positions.asset_class are
# shared across every bot, so this must be one list, not one per bot.
#
# Kept deliberately broad: a symbol wrongly called 'equity' is a silent
# mis-categorisation in cross-bot reporting, whereas a symbol wrongly called
# 'etf' is obvious the moment you look at it. When adding symbols to a bot's
# universe, add any ETFs here too.

ETF_SYMBOLS: frozenset[str] = frozenset({
    # Broad market
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "SCHB", "SCHD",
    "RSP", "MDY", "IJH", "IJR", "EFA", "EEM",
    # Sector SPDRs
    "XLK", "XLF", "XLE", "XLY", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # Fixed income / cash-like
    "SGOV", "BND", "TLT", "IEF", "SHY", "HYG", "LQD", "TIP", "AGG", "BIL",
    # Commodities / FX
    "GLD", "SLV", "USO", "UUP",
    # High-beta / thematic that show up in short screens
    "ARKK", "TQQQ", "SQQQ", "SOXL", "SOXX", "SMH",
})


def classify_asset_class(symbol: str) -> str:
    """Return 'etf' or 'equity' for a symbol.

    Replaces three divergent `_classify` implementations. Crypto, bonds and
    options are NOT inferred from the ticker — those bots know their own
    asset class and pass it explicitly, because inferring 'BTC' -> crypto
    from a string would be guesswork that breaks the moment a ticker
    collides.
    """
    return "etf" if (symbol or "").strip().upper() in ETF_SYMBOLS else "equity"


# ─────────────────────────────────────────────────────────────────────────────
#  Bar / series maths
# ─────────────────────────────────────────────────────────────────────────────
#
# Bars come from Public as a list of dicts. Every screener needs the same
# handful of reductions over the close series.


def closes(bars: List[Dict[str, Any]]) -> List[float]:
    """Extract the close series from Public bars, skipping malformed rows.

    Deliberately lenient: a single bad bar should thin the series, not raise
    and kill the cycle.
    """
    out: List[float] = []
    for b in bars or []:
        try:
            out.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def sma(values: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` values. None if short."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def rolling_low(values: List[float], period: int) -> Optional[float]:
    """Lowest value over the last `period`. None if short."""
    if period <= 0 or len(values) < period:
        return None
    return min(values[-period:])


def rolling_high(values: List[float], period: int) -> Optional[float]:
    """Highest value over the last `period`. None if short."""
    if period <= 0 or len(values) < period:
        return None
    return max(values[-period:])


def pct_return(values: List[float], lookback: int) -> Optional[float]:
    """Fractional return over `lookback` periods (0.05 == +5%).

    None when there is insufficient history or the starting price is
    non-positive (which would make the ratio meaningless rather than just
    large).
    """
    if lookback <= 0 or len(values) < lookback + 1:
        return None
    start, end = values[-(lookback + 1)], values[-1]
    if start <= 0:
        return None
    return (end / start) - 1.0


__all__ = [
    # env
    "env_float",
    "env_int",
    "env_bool",
    "env_list",
    # classification
    "ETF_SYMBOLS",
    "classify_asset_class",
    # series
    "closes",
    "sma",
    "rolling_low",
    "rolling_high",
    "pct_return",
]
