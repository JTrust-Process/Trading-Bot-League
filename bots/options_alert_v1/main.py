"""bots/options_alert_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. Per cycle:

  1. Start a League run.
  2. For each symbol in screener.UNIVERSE:
       a. Fetch daily bars from Public.
       b. Call screener.derive_idea() to map regime → strategy suggestion.
       c. Write one bot_signals row (signal_type='options_idea', direction='NEUTRAL',
          approval_required=True so the future approval queue can pick this up).
  3. Emit a summary OPTIONS_SCAN event in bot_events.
  4. End the run.

Research-only. No order placement. No paper fills. No bot_positions writes.
The output is purely informational ideas a human can act on (or not).
"""

from __future__ import annotations

import os
import sys
import traceback
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
from bots.options_alert_v1 import screener


# ── Config ──────────────────────────────────────────────────────────────────


BARS_PERIOD = os.getenv("OPTIONS_BARS_PERIOD", "YEAR")  # YEAR ≈ 252 bars — exactly
                                                         # what we need for SMA200 + vol baseline

_OVERRIDE = os.getenv("OPTIONS_SYMBOLS", "").strip()
SYMBOLS: tuple[str, ...] = (
    tuple(s.strip().upper() for s in _OVERRIDE.split(",") if s.strip())
    if _OVERRIDE else screener.UNIVERSE
)


def _classify(symbol: str) -> str:
    etfs = {"SPY", "QQQ", "IWM"}
    return "etf" if symbol in etfs else "equity"


def run_cycle() -> str:
    final_status = "success"
    error_count = 0
    idea_count = 0

    run_id = league.start_run("cron")
    print(f"[options] league run_id={run_id}")
    print(f"[options] universe={list(SYMBOLS)} period={BARS_PERIOD}")

    summary_lines = []
    by_strategy: dict[str, int] = {}

    try:
        for sym in SYMBOLS:
            try:
                bars = get_public_bars(sym, period=BARS_PERIOD)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                error_count += 1
                print(f"[options] {sym}: bars fetch raised {e!r}; skipping")
                continue

            if bars is None:
                error_count += 1
                print(f"[options] {sym}: bars fetch returned None; skipping")
                continue
            if not bars:
                print(f"[options] {sym}: empty bars; skipping")
                continue

            idea: Optional[screener.Idea] = screener.derive_idea(sym, bars)
            if idea is None:
                print(f"[options] {sym}: insufficient bars for regime + vol baseline; skipping")
                continue

            league.log_signal(
                signal_type="options_idea",
                symbol=idea.symbol,
                asset_class=_classify(idea.symbol),
                direction="NEUTRAL",            # strategy suggestion, not a directional bet
                confidence=idea.confidence,
                rationale=idea.rationale,
                source="rules",
                approval_required=True,         # any future actor must be human-approved
                metadata={
                    "strategy":     idea.strategy,
                    "trend":        idea.trend,
                    "vol_bucket":   idea.vol_bucket,
                    "realized_vol": idea.realized_vol,
                    "baseline_vol": idea.baseline_vol,
                    "vol_ratio":    idea.vol_ratio,
                    **idea.metrics,
                },
                run_id=run_id,
            )
            idea_count += 1
            by_strategy[idea.strategy] = by_strategy.get(idea.strategy, 0) + 1
            summary_lines.append(
                f"  {sym:<6} regime={idea.trend:<5}/{idea.vol_bucket:<8} "
                f"strategy={idea.strategy:<22} conf={idea.confidence:.2f}"
            )

        print("[options] survey:")
        for line in summary_lines:
            print(line)

        league.log_event(
            event_type="OPTIONS_SCAN",
            message=(
                f"Scanned {idea_count}/{len(SYMBOLS)} symbols. "
                + ", ".join(f"{k}={v}" for k, v in by_strategy.items())
            ),
            metadata={
                "symbols":     list(SYMBOLS),
                "idea_count":  idea_count,
                "by_strategy": by_strategy,
                "bars_period": BARS_PERIOD,
            },
            run_id=run_id,
        )

        if idea_count == 0:
            final_status = "warning"
        elif error_count > 0:
            final_status = "warning"
        return final_status

    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        final_status = "failed"
        error_count += 1
        return final_status

    finally:
        try:
            league.end_run(
                run_id=run_id,
                status=final_status,
                trade_count=0,    # research bot — no trades, ever
                error_count=error_count,
            )
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    status = run_cycle()
    print(f"[options] cycle status={status}")
    sys.exit(0)
