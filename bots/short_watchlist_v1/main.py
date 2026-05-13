"""bots/short_watchlist_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. The bot:

  1. Starts a League run.
  2. For each symbol in screener.UNIVERSE:
       a. Fetch daily bars from Public.
       b. If we already have an open paper short on this symbol, run the
          exit detector. On any exit rule trigger, log a SHORT-exit signal,
          log a COVER trade, and close the paper position.
       c. Otherwise run the entry detector. On a fresh bearish setup, log
          a SHORT signal, log a SHORT trade, and open a paper position.
  3. Ends the run.

NEVER calls a live order endpoint. NEVER touches Public's order surface.
All trades are paper: side='SHORT' on entry, side='COVER' on exit, with
is_paper=True. Positions carry metadata={"direction":"short"} so the
dashboard can render them distinctly.

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
                # No open position — look for a fresh setup.
                entry_sig = screener.detect_entry(sym, bars)
                if entry_sig is None:
                    print(f"[short] {sym}: no entry signal")
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
