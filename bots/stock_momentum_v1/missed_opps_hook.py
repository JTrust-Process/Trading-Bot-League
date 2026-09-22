"""missed_opps_hook — the ONLY thing bot.py is allowed to call for Phase 1.

This is a deliberately paranoid wrapper around league_core.missed_opps. It
exists so that bot.py's entry loop touches exactly one local symbol whose
entire contract is "returns None, never raises, never blocks".

WHY A SEPARATE WRAPPER AT ALL

    bot.py is the file that places real orders. Every bug that has cost
    real money in this system came from something incidental being added
    to, or standing in the way of, a decision path in this file:

        MIN_HOLD_DAYS        skipped every exit for 2 days per position
        MAX_TRADES_PER_DAY   skipped every exit once 5 orders had gone out
        the ATR gate         permanently disabled stops on low-vol symbols
        should_halt_symbol   skipped exits for up to 3 days
        circuit breaker      (crypto) skipped stop-loss unrecoverably

    None of those threw an exception. Each was a small, plausible addition
    near a decision. An analytics hook is exactly that shape of change, so
    it gets its own module, its own import guard, and its own catch-all —
    and it is placed where it can only observe a decision already made.

GUARANTEES

    * Returns None. Always. There is no return value bot.py could branch
      on even by mistake.
    * Never raises. Catches BaseException, including a failed import.
    * No control flow. Does not sleep, retry, loop, or block beyond the
      3s socket timeout inside league_core.missed_opps.
    * Off unless MISSED_OPP_TRACKING is explicitly on, checked twice —
      once here (cheap, avoids the import entirely) and once inside the
      League helper (authoritative).
    * Imports nothing that can place an order.

WHAT THIS MODULE MUST NEVER BECOME

    If a future phase wants AI scoring, it does NOT go here. This hook
    runs inside the live entry loop. Scoring belongs in a separate bot on
    its own schedule, writing bot_signals, with bot_type='agent_research'
    so risk.py structurally refuses it an order path.
"""

from __future__ import annotations

from typing import Any, Optional

# Import is attempted once at module load and cached. A failure here (path
# problem, partial deploy, syntax error downstream) degrades this module to
# a permanent no-op rather than raising inside the trading cycle.
try:
    from league_core import missed_opps as _mo
except BaseException as _e:  # noqa: BLE001
    _mo = None  # type: ignore[assignment]
    print(f"[missed_opps_hook] disabled — league_core.missed_opps "
          f"unavailable: {_e!r}")


def _enabled() -> bool:
    """Cheap local gate so an unset flag skips even the helper call."""
    try:
        import os
        return os.getenv("MISSED_OPP_TRACKING", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except BaseException:
        return False


def record_skipped_candidate(
    symbol: str,
    reason: str,
    *,
    price: Optional[float] = None,
    score: Optional[float] = None,
    regime: Optional[str] = None,
    rank: Optional[int] = None,
    signal_type: Optional[str] = None,
    indicators: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Record a candidate the bot declined. Fire-and-forget.

    Returns None unconditionally. Raises nothing, ever.

    `price` is usually None by design: the rejection point in bot.py sits
    before get_daily_bars() for that symbol, and fetching a price for a
    symbol the bot already rejected would add a network call to the entry
    loop. The scorer resolves the baseline close from history instead.

    CORRECTED 2026-09-22. The paragraph above is true for the MOMENTUM
    path and was wrong as a blanket claim. On the BREAKOUT path,
    check_breakout has already run by the time a candidate is rejected, so
    breakout_result.price is in scope and costs nothing to pass. The call
    site now passes it.

    The original claim caused an eleven-day gap where every row had a null
    price while the very same number was visible inside
    indicators.breakout_reason ("price=333.08 below threshold=341.28").
    A docstring asserting a value is unavailable is not evidence that it
    is; this one was written from the momentum path and never rechecked
    against the branch beside it.

    price is still None whenever the breakout check did not run — symbols
    outside MOMENTUM_SYMBOLS — and that null is honest. No market-data
    fetch has been added to the entry loop.
    """
    try:
        if _mo is None or not _enabled():
            return None
        _mo.safe_insert_missed_opportunity(
            symbol=symbol,
            skip_reason=reason,
            bot_name="stock_momentum_v1",
            run_id=run_id,
            price=price,
            score=score,
            regime=regime,
            rank=rank,
            signal_type=signal_type,
            indicators=indicators,
            metadata=metadata,
        )
    except BaseException as e:  # noqa: BLE001 - must never reach bot.py
        try:
            print(f"[missed_opps_hook] ignored: {e!r}")
        except BaseException:
            pass
    return None


__all__ = ["record_skipped_candidate"]
