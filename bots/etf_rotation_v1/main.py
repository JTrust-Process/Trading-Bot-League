"""bots/etf_rotation_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. The bot:

  1. Loads state.json (last target set + paper capital).
  2. Starts a League run; heartbeat 'starting'.
  3. Fetches SPY bars from Public.com via league_core.public_bars.
  4. Derives the regime and target allocation via strategy.derive_plan.
  5. If target set differs from last_target_set: rebalance.
       - Close any open positions for symbols NOT in the new target.
       - Open positions for symbols in the new target (equal-weight $).
       All trades are simulated at the latest bar close. is_paper=True.
  6. Heartbeat 'idle' / end run.
  7. Save state.json.

No live order placement. No real money. Read-only against Public's bars
endpoint; write-only against the League Supabase project. Any unreachable
dependency (Public auth, Public bars, Supabase) is logged and the cycle
exits cleanly so the next cron run can try again.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Local imports — execute load_dotenv FIRST so all subsequent os.getenv reads
# see the .env file values when running locally.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # GHA injects env vars directly; .env loading is optional

# Make the repo root importable so `from league_core...` and `from bots...` work.
# main.py lives at bots/etf_rotation_v1/main.py, so the root is 2 parents up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from league_core import status as league
from league_core.public_bars import get_public_bars, latest_close
from bots.etf_rotation_v1 import strategy
from bots.etf_rotation_v1 import state as bot_state


# ── Config (env-driven) ─────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


PAPER_CAPITAL_DEFAULT = _env_float("ETF_PAPER_CAPITAL", 1000.0)
BARS_PERIOD = os.getenv("ETF_BARS_PERIOD", "YEAR")  # YEAR ≈ 252 daily bars → enough for SMA(50)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fetch_close(symbol: str) -> Optional[float]:
    """Fetch latest close for a single ETF. None on failure."""
    bars = get_public_bars(symbol, period=BARS_PERIOD)
    if not bars:
        return None
    return latest_close(bars)


def _close_position(
    run_id: Optional[str],
    symbol: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    reason: str,
) -> Tuple[float, float]:
    """Simulate a SELL — close the open bot_position and log a bot_trade.
    Returns (pnl_usd, pnl_pct)."""
    pnl_usd = (exit_price - entry_price) * qty
    pnl_pct = ((exit_price / entry_price) - 1.0) if entry_price > 0 else 0.0
    league.log_trade(
        symbol=symbol,
        side="SELL",
        asset_class="etf",
        quantity=qty,
        price=exit_price,
        amount_usd=exit_price * qty,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        reason=reason,
        strategy="etf_rotation_v1",
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


def _open_position(
    run_id: Optional[str],
    symbol: str,
    dollars: float,
    price: float,
    reason: str,
) -> float:
    """Simulate a BUY — log a bot_trade and create an open bot_position.
    Returns the qty actually allocated (may be 0 if price unavailable)."""
    if price <= 0 or dollars <= 0:
        return 0.0
    qty = dollars / price
    league.log_trade(
        symbol=symbol,
        side="BUY",
        asset_class="etf",
        quantity=qty,
        price=price,
        amount_usd=dollars,
        reason=reason,
        strategy="etf_rotation_v1",
        is_paper=True,
        run_id=run_id,
    )
    league.upsert_position(
        symbol=symbol,
        asset_class="etf",
        status="open",
        quantity=qty,
        entry_price=price,
        entry_at=_utcnow_iso(),
        amount_usd=dollars,
        is_paper=True,
    )
    return qty


# ── Core ────────────────────────────────────────────────────────────────────


def run_cycle() -> str:
    """Run one full cycle. Returns the final League run status."""
    final_status = "success"
    error_count = 0
    trade_count = 0

    s = bot_state.load_state(default_capital=PAPER_CAPITAL_DEFAULT)
    print(f"[etf] state loaded: last_target={s['last_target_set']!r} "
          f"capital=${s['paper_capital']:.2f}")

    run_id = league.start_run("cron")
    print(f"[etf] league run_id={run_id}")

    try:
        spy_bars = get_public_bars("SPY", period=BARS_PERIOD)
        if not spy_bars:
            print("[etf] WARN: could not fetch SPY bars; aborting cycle")
            league.log_event(
                "BARS_FETCH_FAILED",
                symbol="SPY",
                message="Public bars unavailable; skipping rebalance.",
                run_id=run_id,
            )
            final_status = "warning"
            error_count += 1
            return final_status

        plan = strategy.derive_plan(spy_bars)
        target_set = sorted(plan.target_weights.keys())
        print(f"[etf] regime={plan.regime} reason={plan.regime_reason!r}")
        print(f"[etf] target_set={target_set!r}")

        # Always emit a snapshot event so the dashboard has timeline data.
        league.log_event(
            "REGIME_CHECK",
            message=plan.regime_reason,
            metadata={
                "regime": plan.regime,
                "spy_close": plan.spy_close,
                "spy_sma": plan.spy_sma,
                "target": target_set,
            },
            run_id=run_id,
        )

        if plan.regime == "unknown":
            # We don't trade when we can't classify the market.
            print("[etf] regime=unknown; no rebalance")
            return "warning" if final_status != "failed" else final_status

        if target_set == s["last_target_set"]:
            print("[etf] target unchanged; no rebalance")
            return final_status

        # ── Rebalance ──
        print(f"[etf] REGIME CHANGE — rebalancing "
              f"{s['last_target_set']!r} -> {target_set!r}")
        league.log_event(
            "REGIME_CHANGE",
            message=f"{s['last_target_set']} -> {target_set}",
            metadata={"regime": plan.regime, "target": target_set},
            run_id=run_id,
        )

        # Step 1: close every previously-held symbol that's no longer in target.
        # We assume the open bot_positions rows from past cycles are still
        # authoritative; we don't read positions from anywhere else.
        prev = set(s["last_target_set"])
        new = set(target_set)
        to_close = sorted(prev - new)
        to_open  = sorted(new - prev)
        # On the first ever cycle prev is empty, so to_open == sorted(new) — the
        # full target set, opened fresh. On a regime change the two baskets in
        # this bot don't overlap (RISK_ON vs RISK_OFF), so to_close is the entire
        # old basket and to_open is the entire new basket.

        # We need each previously-held symbol's open row to compute PnL.
        # The simplest approach: for each to_close symbol, look up the open
        # position via league._get_open_position. We don't import the helper
        # directly — we use close_position which handles "no open row" as
        # a no-op safely.
        for sym in to_close:
            price = _fetch_close(sym)
            if price is None:
                print(f"[etf]   close {sym}: price unavailable; recording exit without PnL")
                league.close_position(symbol=sym, close_reason="regime_change_no_price")
                trade_count += 1
                continue
            # We need entry/quantity to compute pnl; pull it via the internal helper.
            # Falling back to close_position-without-pnl if not found.
            cfg = league._config()  # noqa: SLF001 - intentional reuse
            pos = league._get_open_position(cfg, sym) if cfg else None  # noqa: SLF001
            if pos and pos.get("quantity") and pos.get("entry_price"):
                pnl_usd, pnl_pct = _close_position(
                    run_id, sym,
                    qty=float(pos["quantity"]),
                    entry_price=float(pos["entry_price"]),
                    exit_price=price,
                    reason="regime_change",
                )
                print(f"[etf]   close {sym} @ {price:.2f} pnl=${pnl_usd:.2f} ({pnl_pct*100:+.2f}%)")
            else:
                # No prior open row found — just record the close attempt.
                league.log_trade(
                    symbol=sym, side="SELL", asset_class="etf",
                    price=price, reason="regime_change_no_prior_position",
                    strategy="etf_rotation_v1", is_paper=True, run_id=run_id,
                )
                print(f"[etf]   close {sym} @ {price:.2f} (no prior open row)")
            trade_count += 1

        # Step 2: open each new-target symbol with an equal share of capital.
        per_symbol = s["paper_capital"] / float(len(target_set)) if target_set else 0.0
        for sym in to_open:
            price = _fetch_close(sym)
            if price is None or price <= 0:
                print(f"[etf]   open {sym}: price unavailable; skipping")
                error_count += 1
                continue
            qty = _open_position(run_id, sym, per_symbol, price, reason="regime_change")
            print(f"[etf]   open  {sym} ${per_symbol:.2f} -> {qty:.6f} units @ {price:.2f}")
            trade_count += 1

        # Persist the new target set.
        s["last_target_set"]   = target_set
        s["last_rebalance_at"] = _utcnow_iso()
        return final_status

    except Exception as e:  # noqa: BLE001 - guard the cycle
        traceback.print_exc()
        final_status = "failed"
        error_count += 1
        try:
            cfg = league._config()  # noqa: SLF001
            if cfg is not None:
                # Direct error row — log_event is a softer surface.
                pass
        except Exception:  # noqa: BLE001
            pass
        return final_status

    finally:
        # Always update state and close out the run.
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
    print(f"[etf] cycle status={status}")
    # GHA treats non-zero exit as workflow failure; we intentionally exit 0
    # even on internal warnings so the schedule keeps running. failed status
    # is recorded in bot_runs / bot_status for visibility.
    sys.exit(0)
