"""bots/short_watchlist_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. The bot:

  1. Starts a League run.
  2. Fetches SPY bars, derives the broader-market regime, and (if the
     regime gate is enabled) suppresses ALL new entries when SPY is in
     a bull regime. Existing positions are ALWAYS managed regardless
     of regime — the exit rules are the working part.
  3. For each symbol in screener.UNIVERSE:
       a. Fetch daily bars from Public.
       b. If we already have an open paper short on this symbol, run the
          exit detector. On any exit rule trigger, log a SHORT-exit signal,
          log a COVER trade, and close the paper position.
       c. Otherwise (and only if the regime gate lets entries through):
          - Run the entry detector (screener rules).
          - Apply the min-confidence filter (SHORT_MIN_CONFIDENCE).
          - Apply the relative-weakness filter vs SPY 3m return
            (SHORT_REL_WEAKNESS_PCT).
          - If all pass, log a SHORT signal, log a SHORT trade, and open
            a paper position.
  4. Ends the run.

NEVER calls a live order endpoint. NEVER touches Public's order surface.
All trades are paper: side='SHORT' on entry, side='COVER' on exit, with
is_paper=True. Positions carry metadata={"direction":"short"} so the
dashboard can render them distinctly.

Entry-gate env vars (added 2026-07-24 after a paper-perf review showed
the strategy losing money in a persistent bull regime — the gates
prevent entries in the wrong environment without touching the working
exit logic; existing positions are unaffected):

  SHORT_REGIME_GATE        Enable/disable the SPY regime gate. Default 'true'.
                           When true, no new shorts are opened while
                           SPY close > SPY SMA(SHORT_REGIME_SMA), i.e.
                           bull regime. Also treated as "skip" if SPY
                           bars can't be fetched (fail-safe).
  SHORT_REGIME_SMA         SMA period for the regime gate. Default 200.
  SHORT_MIN_CONFIDENCE     Min entry confidence to open a short.
                           Default 0.70. Screener returns [0.5, 1.0];
                           the old bot took every setup >= 0.5 which is
                           part of why paper performance was poor.
  SHORT_REL_WEAKNESS_PCT   Minimum underperformance vs SPY 3m return
                           required to short a symbol. Default 0.05 (5%).
                           Symbol's 3m return must be worse than SPY's
                           by at least this much. Prevents shorting
                           strong momentum leaders during index rallies.

Exits 0 even on warnings — the schedule should keep running. Run status
is recorded in bot_runs for visibility.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from league_core import status as league
from league_core.public_bars import get_public_bars
from bots.short_watchlist_v1 import screener
from bots.short_watchlist_v1 import state as bot_state


# ── Config ──────────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


CAPITAL_PER_TRADE = _env_float("SHORT_CAPITAL_PER_TRADE", 100.0)
BARS_PERIOD = os.getenv("SHORT_BARS_PERIOD", "YEAR")

_OVERRIDE = os.getenv("SHORT_SYMBOLS", "").strip()
SYMBOLS = (
    tuple(s.strip().upper() for s in _OVERRIDE.split(",") if s.strip())
    if _OVERRIDE else screener.UNIVERSE
)


# ── Entry gates (added 2026-07-24; see module docstring for rationale) ─────

def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


REGIME_GATE_ENABLED = _env_bool("SHORT_REGIME_GATE",  True)
REGIME_SMA_PERIOD   = int(os.getenv("SHORT_REGIME_SMA", "200") or "200")
MIN_CONFIDENCE      = _env_float("SHORT_MIN_CONFIDENCE",    0.70)
REL_WEAKNESS_PCT    = _env_float("SHORT_REL_WEAKNESS_PCT",  0.05)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _classify(symbol: str) -> str:
    """Mirror the ETF bot's tiny allow-list — anything not an ETF defaults
    to 'equity' so bot_trades.asset_class is informative."""
    etfs = {"QQQ", "SPY", "IWM", "XLK"}
    return "etf" if symbol in etfs else "equity"


def _open_paper_short(
    run_id: Optional[str],
    sig: screener.EntrySignal,
    capital: float,
) -> int:
    """Open a paper short at sig.close. Returns 1 if a trade row was written."""
    if sig.close <= 0 or capital <= 0:
        return 0
    qty = capital / sig.close

    league.log_signal(
        signal_type="short_setup",
        symbol=sig.symbol,
        asset_class=_classify(sig.symbol),
        direction="SHORT",
        confidence=sig.confidence,
        suggested_size_usd=capital,
        rationale=sig.rationale,
        source="rules",
        approval_required=False,   # paper-only, not gated
        metadata={
            "close":       sig.close,
            "sma50":       sig.sma50,
            "sma200":      sig.sma200,
            "rolling_low": sig.rolling_low,
            "ret_3m":      sig.ret_3m,
        },
        run_id=run_id,
    )
    league.log_trade(
        symbol=sig.symbol,
        side="SHORT",
        asset_class=_classify(sig.symbol),
        quantity=qty,
        price=sig.close,
        amount_usd=capital,
        reason="bearish_setup",
        strategy="short_watchlist_v1",
        is_paper=True,
        run_id=run_id,
    )
    league.upsert_position(
        symbol=sig.symbol,
        asset_class=_classify(sig.symbol),
        status="open",
        quantity=qty,
        entry_price=sig.close,
        entry_at=_utcnow_iso(),
        amount_usd=capital,
        is_paper=True,
        metadata={
            "direction":  "short",
            "confidence": sig.confidence,
        },
    )
    return 1


def _close_paper_short(
    run_id: Optional[str],
    symbol: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    reason: str,
) -> tuple[float, float]:
    """Close a paper short. PnL convention for shorts:
        pnl_usd = (entry - exit) * qty
        pnl_pct = (entry - exit) / entry
    Positive PnL when price fell after entry."""
    if entry_price <= 0:
        pnl_usd = 0.0
        pnl_pct = 0.0
    else:
        pnl_usd = (entry_price - exit_price) * qty
        pnl_pct = (entry_price - exit_price) / entry_price

    league.log_signal(
        signal_type="short_exit",
        symbol=symbol,
        asset_class=_classify(symbol),
        direction="EXIT",
        rationale=f"{reason}: exit @ {exit_price:.2f} vs entry {entry_price:.2f}",
        source="rules",
        metadata={"reason": reason, "entry": entry_price, "exit": exit_price},
        run_id=run_id,
    )
    league.log_trade(
        symbol=symbol,
        side="COVER",
        asset_class=_classify(symbol),
        quantity=qty,
        price=exit_price,
        amount_usd=exit_price * qty,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        reason=reason,
        strategy="short_watchlist_v1",
        is_paper=True,
        run_id=run_id,
    )
    league.close_position(
        symbol=symbol,
        exit_price=exit_price,
        exit_at=_utcnow_iso(),
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        close_reason=reason,
    )
    return pnl_usd, pnl_pct


# ── Market context (added 2026-07-24 — see module docstring) ───────────────


def _market_context(bars_period: str) -> dict:
    """Fetch SPY bars and derive the broader-market context used to gate
    entries. Called once per cycle; result is reused for every symbol.

    Returned dict keys:
      spy_close     — latest SPY close, or None if fetch failed
      spy_sma       — SPY SMA(REGIME_SMA_PERIOD), or None if too few bars
      spy_ret_3m    — SPY 3-month return, or None if too few bars
      regime        — 'bull' | 'bear' | 'unknown'
      skip_entries  — True when this cycle should not open ANY new shorts

    Fail-safe: if SPY data can't be fetched OR the regime gate is
    enabled and regime is bull/unknown, skip_entries becomes True.
    Existing positions are unaffected — the exit path in run_cycle runs
    for every open position regardless of ctx.
    """
    ctx: dict = {
        "spy_close":    None,
        "spy_sma":      None,
        "spy_ret_3m":   None,
        "regime":       "unknown",
        "skip_entries": False,
    }

    try:
        bars = get_public_bars("SPY", period=bars_period)
    except Exception:  # noqa: BLE001
        bars = None
    if not bars:
        # Can't measure — fail-safe: if the gate is enabled, skip entries.
        ctx["skip_entries"] = REGIME_GATE_ENABLED
        return ctx

    closes: list[float] = []
    for b in bars:
        try:
            closes.append(float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not closes:
        ctx["skip_entries"] = REGIME_GATE_ENABLED
        return ctx

    ctx["spy_close"] = float(closes[-1])

    if len(closes) >= REGIME_SMA_PERIOD:
        sma = sum(closes[-REGIME_SMA_PERIOD:]) / float(REGIME_SMA_PERIOD)
        ctx["spy_sma"] = sma
        ctx["regime"]  = "bull" if ctx["spy_close"] > sma else "bear"

    # Reuse the screener's lookback so the relative-weakness comparison
    # is apples-to-apples with what detect_entry computes per symbol.
    if len(closes) >= screener.MOMENTUM_LOOKBACK + 1:
        start = closes[-(screener.MOMENTUM_LOOKBACK + 1)]
        end   = closes[-1]
        if start > 0:
            ctx["spy_ret_3m"] = (end / start) - 1.0

    if REGIME_GATE_ENABLED and ctx["regime"] in ("bull", "unknown"):
        ctx["skip_entries"] = True

    return ctx


# ── Core ────────────────────────────────────────────────────────────────────


def run_cycle() -> str:
    final_status = "success"
    error_count = 0
    trade_count = 0

    s = bot_state.load_state(default_capital=CAPITAL_PER_TRADE)
    print(f"[short] capital_per_trade=${s['paper_short_capital_per_trade']:.2f}")
    print(f"[short] universe={list(SYMBOLS)} period={BARS_PERIOD}")

    run_id = league.start_run("cron")
    print(f"[short] league run_id={run_id}")

    # Market context (regime + relative-weakness reference). Computed
    # ONCE per cycle and reused when filtering entries below. Existing
    # positions are unaffected — exit path runs unconditionally.
    ctx = _market_context(BARS_PERIOD)
    print(f"[short] SPY close={ctx['spy_close']} sma{REGIME_SMA_PERIOD}={ctx['spy_sma']} "
          f"regime={ctx['regime']} spy_ret_3m={ctx['spy_ret_3m']} "
          f"skip_entries={ctx['skip_entries']}")
    league.log_event(
        event_type="MARKET_CONTEXT",
        message=f"regime={ctx['regime']} skip_entries={ctx['skip_entries']}",
        metadata={
            "spy_close":         ctx["spy_close"],
            "spy_sma":           ctx["spy_sma"],
            "spy_ret_3m":        ctx["spy_ret_3m"],
            "regime":            ctx["regime"],
            "regime_sma_period": REGIME_SMA_PERIOD,
            "regime_gate":       REGIME_GATE_ENABLED,
            "min_confidence":    MIN_CONFIDENCE,
            "rel_weakness_pct":  REL_WEAKNESS_PCT,
            "skip_entries":      ctx["skip_entries"],
        },
        run_id=run_id,
    )

    try:
        opened = 0
        closed = 0
        for sym in SYMBOLS:
            try:
                bars = get_public_bars(sym, period=BARS_PERIOD)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                error_count += 1
                print(f"[short] {sym}: bars fetch raised {e!r}; skipping")
                continue

            if bars is None:
                error_count += 1
                print(f"[short] {sym}: bars fetch returned None; skipping")
                continue
            if not bars:
                print(f"[short] {sym}: empty bars; skipping")
                continue

            # Are we currently short this symbol on paper?
            cfg = league._config()  # noqa: SLF001
            pos = league._get_open_position(cfg, sym) if cfg else None  # noqa: SLF001

            if pos and pos.get("quantity") and pos.get("entry_price"):
                # Open paper short — check exit conditions.
                entry_price = float(pos["entry_price"])
                qty = float(pos["quantity"])
                exit_sig = screener.detect_exit(sym, bars, entry_price=entry_price)
                if exit_sig is None:
                    print(f"[short] {sym}: holding short, no exit trigger")
                    continue
                pnl_usd, pnl_pct = _close_paper_short(
                    run_id, sym, qty,
                    entry_price=entry_price,
                    exit_price=exit_sig.close,
                    reason=exit_sig.reason,
                )
                trade_count += 1
                closed += 1
                print(
                    f"[short] {sym}: COVER ({exit_sig.reason}) "
                    f"@ {exit_sig.close:.2f}  pnl=${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%)"
                )
            else:
                # No open position — look for a fresh setup, filtered by
                # regime gate + min confidence + relative weakness. Any
                # filter fail = continue (no trade this cycle).
                if ctx["skip_entries"]:
                    print(f"[short] {sym}: entries suppressed by regime gate "
                          f"(regime={ctx['regime']}); skipping")
                    continue
                entry_sig = screener.detect_entry(sym, bars)
                if entry_sig is None:
                    print(f"[short] {sym}: no entry signal")
                    continue
                if entry_sig.confidence < MIN_CONFIDENCE:
                    print(f"[short] {sym}: entry conf {entry_sig.confidence:.2f} < "
                          f"min {MIN_CONFIDENCE:.2f}; skipping")
                    continue
                if ctx["spy_ret_3m"] is not None:
                    required = ctx["spy_ret_3m"] - REL_WEAKNESS_PCT
                    if entry_sig.ret_3m >= required:
                        print(f"[short] {sym}: 3m ret {entry_sig.ret_3m*100:+.2f}% "
                              f"not weaker than SPY {ctx['spy_ret_3m']*100:+.2f}% by "
                              f"{REL_WEAKNESS_PCT*100:.2f}%; skipping")
                        continue
                wrote = _open_paper_short(
                    run_id, entry_sig,
                    capital=float(s["paper_short_capital_per_trade"]),
                )
                if wrote:
                    trade_count += 1
                    opened += 1
                    print(
                        f"[short] {sym}: OPEN paper short @ {entry_sig.close:.2f} "
                        f"conf={entry_sig.confidence:.2f}"
                    )

        # Cycle summary event
        league.log_event(
            event_type="SHORT_WATCH_SURVEY",
            message=f"Scanned {len(SYMBOLS)} symbols. Opened {opened}, closed {closed}.",
            metadata={
                "symbols":  list(SYMBOLS),
                "opened":   opened,
                "closed":   closed,
                "trades":   trade_count,
                "errors":   error_count,
            },
            run_id=run_id,
        )

        s["last_run_at"] = _utcnow_iso()

        if error_count > 0:
            final_status = "warning"
        return final_status

    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        final_status = "failed"
        error_count += 1
        return final_status

    finally:
        try:
            bot_state.save_state(s)
        except Exception:  # noqa: BLE001
            pass
        try:
            league.end_run(
                run_id=run_id,
                status=final_status,
                trade_count=trade_count,
                error_count=error_count,
            )
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    status = run_cycle()
    print(f"[short] cycle status={status}")
    sys.exit(0)
