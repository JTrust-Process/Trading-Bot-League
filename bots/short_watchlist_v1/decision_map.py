"""decision_map — screener signals → BotDecision. PURE. DORMANT.

PHASE C, STEP 1. Nothing imports this module. `main.py` is untouched.

This is the adapter between `screener.py`'s existing pure dataclasses and
the dormant `league_core.decisions.BotDecision` contract. It exists on its
own, before any writer and before any schema, because building the mapping
is what forces the modelling questions into the open while every answer is
still free to change. A migration commits those answers; a pure function
lets you argue with them first.

────────────────────────────────────────────────────────────────────────────
WHY THIS BOT FIRST

short_watchlist_v1 imports NO order client — only league_core.status
(logging), league_core.common (pure helpers), league_core.public_bars
(read-only market data), and its own screener/state. Adding a decision
mapper to a module with no broker access cannot create one. It is also
paper by construction rather than by configuration: its functions are named
`_open_paper_short` / `_close_paper_short` and there is no `mode == "live"`
branch to get wrong.

────────────────────────────────────────────────────────────────────────────
THE ACTION MAPPING, AND WHY IT IS NOT ARBITRARY

    short ENTRY  → SELL  + position_effect=OPEN    sell borrowed shares
    short EXIT   → COVER + position_effect=CLOSE   buy them back

Both are ACTIONABLE in the contract, which is correct: each would be a real
order if this bot ever placed one.

The explicit `position_effect=OPEN` on the entry is load-bearing, not
decoration. SELL DEFAULTS to CLOSE in the contract, because every live bot
in this system is long-only and an unannotated SELL is overwhelmingly a
long exit. A short entry is the exception and must say so.

This mapper is what surfaced that ambiguity. Mapping a short entry to a
bare SELL produced a decision reporting `is_close=True` — backwards, and
dangerous in the one case where it matters most: a future executor keying
its entry-throttle exemption on "is this a close?" would have let a short
ENTRY bypass the daily trade cap. Found on a paper bot with no order
client, which is exactly why this bot adopted the contract first.

This makes short_watchlist_v1 the ONLY bot that exercises COVER. Every live
bot is long-only, so COVER has never been produced by anything. Exercising
it first on a bot with zero capital — rather than discovering its edge cases
when a live short bot needs it — is the entire point of adopting here first.

Note the consequence for risk: COVER is in decisions.CLOSE_ACTIONS, so a
future executor must never gate it behind entry-side throttles. Daily trade
caps, cooldowns, minimum hold periods and circuit breakers are ENTRY
concerns. Applying them to exits is how this system disabled its own
stop-losses five separate times.

────────────────────────────────────────────────────────────────────────────
WHAT THIS MODULE DELIBERATELY DOES NOT DO

  * No sizing. `suggested_amount_usd` and `suggested_quantity` are always
    None. The SCREENER does not size — SHORT_CAPITAL_PER_TRADE is an env
    var read by main.py, not a screener output. Passing main.py's notional
    through here would mean the mapper reported a size the signal never
    computed. A honest None beats a plausible number from the wrong layer.

  * No mode. `intended_mode="paper"` is advisory only. League Core resolves
    the authoritative mode from bot_registry at write time.

  * No bot_id authority. `bot_id` is a required contract field so it is
    supplied here, but the Phase C writer MUST override it from
    LEAGUE_BOT_ID in the scheduler's per-job env scope. Otherwise one bot
    can file decisions under another's identity. See BOT_ID_NOTE below.

  * No IO. No Supabase, no network, no clock beyond what the contract
    itself omits (created_at is Core's / the database's to set).

  * No asset-class list of its own. It delegates to
    league_core.common.classify_asset_class. This bot once carried a
    four-symbol allowlist while options_alert_v1 had three and
    stock_momentum_v1 had thirty, so the SAME ticker was recorded with a
    different asset_class depending on which bot logged it. One list.
"""

from __future__ import annotations

from typing import Any, Optional

from league_core import common
from league_core.decisions import CLOSE, COVER, OPEN, SELL, BotDecision
from bots.short_watchlist_v1 import screener


#: Identity carried on mapped decisions.
#:
#: BOT_ID_NOTE: this is a convenience default, NOT an authority claim. The
#: Phase C writer must overwrite it from os.getenv("LEAGUE_BOT_ID") — set by
#: agent_runner's _bot_env_scope() for the job's lifetime — and must REFUSE
#: to write when that variable is absent rather than falling back to this
#: constant. An unattributed decision row is worse than no row: it looks
#: like data. The fallback-on-absence pattern is exactly how crypto_ema_atr
#: silently wrote to a brand-new bot_state row for weeks when GITHUB_REF_NAME
#: did not exist on Fly.
BOT_ID = "short_watchlist_v1"

#: bot_type must be one of league_core.contracts.BOT_TYPES.
BOT_TYPE = "short"

#: Advisory only — see the module docstring. Never a substitute for the
#: registry lookup.
INTENDED_MODE = "paper"

#: Applied to every decision from this bot so downstream analysis can group
#: by strategy without parsing prose.
BASE_TAGS = ("short_watchlist", "mean_reversion_short")


def _clamp_confidence(value: Any) -> Optional[float]:
    """Coerce a screener confidence into the contract's [0, 1] range.

    screener.detect_entry already bounds confidence to [0.5, 1.0] by
    construction, so this should never actually clamp. It is here because
    "should never" is how the -3.4%-against-a-2.5%-stop bug survived for
    months: a bound that is documented but unenforced is not a bound.

    Returns None for anything non-numeric rather than raising — the
    contract treats confidence as optional, and a missing confidence is a
    better outcome than a decision that cannot be constructed at all.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def _require_reason(*candidates: Any) -> str:
    """First non-empty candidate, else raise.

    `reason` is required and must be non-empty: a decision whose rationale
    is blank is unreadable six weeks later, which is precisely when these
    rows get read. Raising here is correct — this is a construction-time
    programming error caught by tests and CI, never a runtime condition
    inside a trading loop.
    """
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    raise ValueError(
        "decision_map: refusing to build a BotDecision with an empty reason; "
        f"candidates were {candidates!r}"
    )


def entry_to_decision(
    sig: screener.EntrySignal,
    symbol: Optional[str] = None,
    run_id: Optional[str] = None,
) -> BotDecision:
    """Map a short-entry signal to a SELL decision.

    SELL because opening a short means selling borrowed shares. The
    contract classes it ACTIONABLE, which is right: if this bot ever placed
    orders, this is the one it would place.

    `symbol` defaults to the signal's own symbol; the parameter exists so a
    caller iterating a universe can pass the canonical ticker it is keyed
    on rather than trusting the signal to echo it back.

    Sizing is intentionally absent — see the module docstring.
    """
    if sig is None:
        raise ValueError("decision_map.entry_to_decision: sig is required")

    sym = (symbol or getattr(sig, "symbol", "") or "").strip()
    if not sym:
        raise ValueError("decision_map.entry_to_decision: no symbol available")

    reason = _require_reason(
        getattr(sig, "rationale", None),
        f"short setup detected on {sym.upper()}",
    )

    # Read-only snapshot of what the screener saw. Diagnostic, never an
    # input to a later decision — reading decisions back to drive control
    # flow recreates the stale-state bug class (a dollar high-water mark
    # surviving across positions; candle counters frozen by a capped list).
    metadata: dict[str, Any] = {
        "screener": "detect_entry",
        "close": getattr(sig, "close", None),
        "sma50": getattr(sig, "sma50", None),
        "sma200": getattr(sig, "sma200", None),
        "rolling_low": getattr(sig, "rolling_low", None),
        "ret_3m": getattr(sig, "ret_3m", None),
        "breakdown_lookback": screener.BREAKDOWN_LOOKBACK,
        "momentum_lookback": screener.MOMENTUM_LOOKBACK,
    }

    return BotDecision(
        bot_id=BOT_ID,
        bot_type=BOT_TYPE,
        asset_class=common.classify_asset_class(sym),
        symbol=sym,
        action=SELL,
        # EXPLICIT. SELL defaults to CLOSE for long-only compatibility, so a
        # short entry must declare that it OPENS. Intent only — League Core
        # must still verify allow_short, mode and capital before any order.
        position_effect=OPEN,
        reason=reason,
        confidence=_clamp_confidence(getattr(sig, "confidence", None)),
        suggested_amount_usd=None,   # screener does not size
        suggested_quantity=None,     # screener does not size
        strategy_tags=BASE_TAGS + ("entry",),
        risk_notes=(
            f"Paper short. Stop at +{screener.ADVERSE_PCT:.0%} adverse move, "
            f"take-profit at -{screener.FAVORABLE_PCT:.0%}. Risk exits are "
            f"evaluated before signal exits in screener.detect_exit."
        ),
        metadata=metadata,
        run_id=run_id,
        intended_mode=INTENDED_MODE,
    )


def exit_to_decision(
    sig: screener.ExitSignal,
    symbol: Optional[str] = None,
    run_id: Optional[str] = None,
    entry_price: Optional[float] = None,
) -> BotDecision:
    """Map a short-exit signal to a COVER decision.

    COVER because closing a short means buying the shares back. It is in
    decisions.CLOSE_ACTIONS, so a future executor must never subject it to
    entry-side throttles.

    `entry_price` is optional and used ONLY to record the realised move in
    metadata. It is never used to decide anything — the screener already
    made the decision, and recomputing it here would create a second
    implementation that could silently disagree with the first.
    """
    if sig is None:
        raise ValueError("decision_map.exit_to_decision: sig is required")

    sym = (symbol or getattr(sig, "symbol", "") or "").strip()
    if not sym:
        raise ValueError("decision_map.exit_to_decision: no symbol available")

    exit_reason = (getattr(sig, "reason", "") or "").strip()
    reason = _require_reason(
        # Prefer the machine-readable reason joined to the human rationale,
        # so neither the grouping key nor the explanation is lost.
        f"{exit_reason}: {getattr(sig, 'rationale', '')}".strip(": ").strip()
        if exit_reason else None,
        getattr(sig, "rationale", None),
        exit_reason,
        f"exit signal on {sym.upper()}",
    )

    metadata: dict[str, Any] = {
        "screener": "detect_exit",
        "exit_reason": exit_reason or None,
        "close": getattr(sig, "close", None),
        "sma20": getattr(sig, "sma20", None),
        "adverse_pct_threshold": screener.ADVERSE_PCT,
        "favorable_pct_threshold": screener.FAVORABLE_PCT,
    }

    # Diagnostic only. For a SHORT, price UP is adverse, so a positive
    # move_pct is a loss — recorded with that sign convention made explicit
    # rather than left for a future reader to infer wrongly.
    if entry_price is not None:
        try:
            ep = float(entry_price)
            close = float(getattr(sig, "close", None))
            if ep > 0 and close == close and ep == ep:
                metadata["entry_price"] = ep
                metadata["move_pct_from_entry"] = round((close - ep) / ep, 6)
                metadata["move_sign_note"] = (
                    "positive = price rose = LOSS on a short"
                )
        except (TypeError, ValueError):
            pass

    # Exit reasons carry different risk meaning. 'stop' is a risk exit and
    # must be distinguishable from a discretionary one at a glance — the
    # AMZN short exited -16.51% against a 5% stop while LOGGED as
    # trend_reversal, which is how the overshoot stayed invisible.
    tags = BASE_TAGS + ("exit",)
    if exit_reason:
        tags = tags + (f"exit_{exit_reason}",)
    if exit_reason == "stop":
        tags = tags + ("risk_exit",)

    return BotDecision(
        bot_id=BOT_ID,
        bot_type=BOT_TYPE,
        asset_class=common.classify_asset_class(sym),
        symbol=sym,
        action=COVER,
        # COVER always closes; stated explicitly so the pair reads
        # symmetrically against entry_to_decision above.
        position_effect=CLOSE,
        reason=reason,
        confidence=None,             # exits are rule-triggered, not scored
        suggested_amount_usd=None,   # position size is main.py's, not the screener's
        suggested_quantity=None,
        strategy_tags=tags,
        risk_notes=(
            "Risk exit — stop breached."
            if exit_reason == "stop"
            else f"Signal exit ({exit_reason or 'unspecified'}). Not a risk limit."
        ),
        metadata=metadata,
        run_id=run_id,
        intended_mode=INTENDED_MODE,
    )


__all__ = [
    "BOT_ID",
    "BOT_TYPE",
    "INTENDED_MODE",
    "BASE_TAGS",
    "entry_to_decision",
    "exit_to_decision",
]
