"""bots/bond_research_v1/screener.py — scoring logic.

Pure functions. No I/O, no Supabase, no Public API calls. Inputs are
bars (list-of-dicts from league_core.public_bars); outputs are typed
Score records. main.py wires the I/O around this.

Score rubric (composite 0..1, higher = stronger):

  trend_score      = +1 if close > SMA200, 0 otherwise         (weight 0.35)
  momentum_score   = clipped 3-month return / 0.10              (weight 0.30)
  stability_score  = 1 - clipped(annualized_vol / 0.15)         (weight 0.20)
  liquidity_score  = 1 if avg_volume_20d > 1M else 0.5          (weight 0.15)

  composite = weighted sum, clipped to [0, 1].

Bucket mapping:
  composite >= 0.70             -> 'keep_active'        (strong)
  0.45 <= composite <  0.70     -> 'reduce_priority'    (decent)
  0.25 <= composite <  0.45     -> 'paper_only'         (marginal)
  composite <  0.25             -> 'remove'             (weak)

The buckets match the stock bot's analyze_backtests.py advisory output
so a future consumer bot can read scores from any research source
uniformly. None of these thresholds are sacred — tune as needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict, List, Optional


# Universe — major bond ETFs across the duration / credit spectrum.
# Hand-edit if you want to expand coverage.
UNIVERSE = (
    "SGOV",  # 0-3 month T-bills (cash proxy)
    "SHY",   # 1-3 yr treasuries
    "IEF",   # 7-10 yr treasuries
    "TLT",   # 20+ yr treasuries
    "LQD",   # investment-grade corporate
    "HYG",   # high-yield corporate
    "TIP",   # inflation-protected treasuries
    "BND",   # total bond market
)


# Scoring weights
W_TREND     = 0.35
W_MOMENTUM  = 0.30
W_STABILITY = 0.20
W_LIQUIDITY = 0.15

# Reference scales for clipping
MOM_REF_PCT     = 0.10   # 10% 3-month return = full credit
VOL_REF_ANN     = 0.15   # 15% annualized vol = zero stability credit
LIQ_VOL_THRESH  = 1_000_000  # 20-day avg volume above this = full liquidity

# Bucket cuts on the composite (higher = stronger)
BUCKET_KEEP        = 0.70
BUCKET_REDUCE      = 0.45
BUCKET_PAPER       = 0.25


# ── Helpers (pure) ──────────────────────────────────────────────────────────


def _closes(bars: List[Dict]) -> List[float]:
    out: List[float] = []
    for b in bars:
        try:
            out.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _volumes(bars: List[Dict]) -> List[float]:
    out: List[float] = []
    for b in bars:
        try:
            out.append(float(b.get("volume", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / float(period)


def _ret_n(values: List[float], n: int) -> Optional[float]:
    if len(values) < n + 1:
        return None
    start, end = values[-(n + 1)], values[-1]
    if start <= 0:
        return None
    return (end / start) - 1.0


def _annualized_vol(values: List[float], lookback: int = 60) -> Optional[float]:
    """Annualized stdev of daily log returns over the most recent lookback days."""
    if len(values) < lookback + 1:
        lookback = max(20, len(values) - 1)
        if lookback < 5:
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
    daily = sqrt(var)
    return daily * sqrt(252.0)


def _avg_volume(volumes: List[float], n: int = 20) -> Optional[float]:
    if not volumes:
        return None
    tail = [v for v in volumes[-n:] if v > 0]
    if not tail:
        return None
    return sum(tail) / float(len(tail))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── Scoring (pure) ──────────────────────────────────────────────────────────


@dataclass
class Score:
    symbol: str
    composite: float
    classification: str
    metrics: Dict[str, Optional[float]]
    notes: str


def _classify(composite: float) -> str:
    if composite >= BUCKET_KEEP:
        return "keep_active"
    if composite >= BUCKET_REDUCE:
        return "reduce_priority"
    if composite >= BUCKET_PAPER:
        return "paper_only"
    return "remove"


def score_symbol(symbol: str, bars: List[Dict]) -> Optional[Score]:
    """Score one symbol from its bars. Returns None if there aren't enough
    bars to make a meaningful judgment (need ~200 for SMA200)."""
    closes = _closes(bars)
    volumes = _volumes(bars)
    if len(closes) < 60:
        # Need at least ~3 months for momentum + volatility to mean anything.
        return None

    sma200 = _sma(closes, 200)
    last = closes[-1]
    trend = None
    if sma200 is not None:
        trend = 1.0 if last > sma200 else 0.0

    mom_3m = _ret_n(closes, 63)   # ~63 trading days = 3 months
    momentum = None
    if mom_3m is not None:
        momentum = _clip(mom_3m / MOM_REF_PCT, 0.0, 1.0)

    vol_ann = _annualized_vol(closes, lookback=60)
    stability = None
    if vol_ann is not None:
        stability = _clip(1.0 - (vol_ann / VOL_REF_ANN), 0.0, 1.0)

    avg_vol = _avg_volume(volumes, n=20)
    liquidity = None
    if avg_vol is not None:
        liquidity = 1.0 if avg_vol >= LIQ_VOL_THRESH else 0.5

    # Weighted composite from whichever components we have. We rescale the
    # weights so missing components don't penalize the score.
    components: List[tuple[float, float]] = []  # (value, weight)
    if trend     is not None: components.append((trend,     W_TREND))
    if momentum  is not None: components.append((momentum,  W_MOMENTUM))
    if stability is not None: components.append((stability, W_STABILITY))
    if liquidity is not None: components.append((liquidity, W_LIQUIDITY))

    if not components:
        return None

    total_weight = sum(w for _, w in components)
    composite = sum(v * w for v, w in components) / total_weight
    composite = _clip(composite, 0.0, 1.0)
    classification = _classify(composite)

    notes_bits: List[str] = []
    if sma200 is not None:
        notes_bits.append(
            f"close {last:.2f} {'>' if last > sma200 else '<='} SMA200 {sma200:.2f}"
        )
    if mom_3m is not None:
        notes_bits.append(f"3m return {mom_3m*100:+.2f}%")
    if vol_ann is not None:
        notes_bits.append(f"ann vol {vol_ann*100:.2f}%")
    if avg_vol is not None:
        notes_bits.append(f"20d avg vol {avg_vol:,.0f}")

    return Score(
        symbol=symbol,
        composite=composite,
        classification=classification,
        metrics={
            "trend":      trend,
            "momentum":   momentum,
            "stability":  stability,
            "liquidity":  liquidity,
            "sma200":     sma200,
            "last_close": last,
            "ret_3m":     mom_3m,
            "ann_vol":    vol_ann,
            "avg_vol_20d": avg_vol,
        },
        notes=" · ".join(notes_bits),
    )


__all__ = ["UNIVERSE", "Score", "score_symbol"]
