"""bots/bond_research_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. The bot:

  1. Starts a League run.
  2. For each symbol in screener.UNIVERSE, fetches Public daily bars.
  3. Calls screener.score_symbol() to compute a composite and classification.
  4. Writes one bot_research_scores row per symbol.
  5. Emits a single SCREENED bot_event with the survey summary.
  6. Ends the run.

This bot does NOT simulate trades, write to bot_trades, write to
bot_positions, or call any Public order endpoint. It is research-only.

Exits 0 even on warnings — the schedule should keep running. The final
run status is recorded in bot_runs for visibility.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Make the repo root importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from league_core import status as league
from league_core.public_bars import get_public_bars
from bots.bond_research_v1 import screener


# ── Config (env-driven) ─────────────────────────────────────────────────────


BARS_PERIOD = os.getenv("BOND_BARS_PERIOD", "YEAR")  # YEAR ≈ 252 bars — fine for SMA200.

# Optional symbol override (comma-separated). Useful for local testing.
_OVERRIDE = os.getenv("BOND_SYMBOLS", "").strip()
SYMBOLS: tuple[str, ...] = (
    tuple(s.strip().upper() for s in _OVERRIDE.split(",") if s.strip())
    if _OVERRIDE else screener.UNIVERSE
)


# ── Core ────────────────────────────────────────────────────────────────────


def run_cycle() -> str:
    final_status = "success"
    error_count = 0
    score_count = 0

    run_id = league.start_run("cron")
    print(f"[bond] league run_id={run_id}")
    print(f"[bond] universe={list(SYMBOLS)} period={BARS_PERIOD}")

    summary_lines: List[str] = []
    classifications: dict[str, int] = {
        "keep_active": 0, "reduce_priority": 0, "paper_only": 0, "remove": 0,
    }

    try:
        for sym in SYMBOLS:
            try:
                bars = get_public_bars(sym, period=BARS_PERIOD)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                error_count += 1
                print(f"[bond] {sym}: bars fetch raised {e!r}; skipping")
                continue

            if bars is None:
                error_count += 1
                print(f"[bond] {sym}: bars fetch returned None; skipping")
                continue
            if not bars:
                print(f"[bond] {sym}: empty bars; skipping")
                continue

            score: Optional[screener.Score] = screener.score_symbol(sym, bars)
            if score is None:
                print(f"[bond] {sym}: insufficient bars for scoring; skipping")
                continue

            # Persist
            league.log_research_score(
                symbol=sym,
                asset_class="etf",   # these are bond ETFs; the underlying is fixed-income
                                     # but Public/the dashboard care about the wrapper type
                score=score.composite,
                classification=score.classification,
                period=BARS_PERIOD,
                metrics=score.metrics,
                notes=score.notes,
                run_id=run_id,
            )
            score_count += 1
            classifications[score.classification] = classifications.get(
                score.classification, 0
            ) + 1
            summary_lines.append(
                f"  {sym:<6} composite={score.composite:.2f} "
                f"-> {score.classification:<16} ({score.notes})"
            )

        print("[bond] survey:")
        for line in summary_lines:
            print(line)

        # Single SCREENED event summarizing the cycle. Easier to scan than
        # one event per symbol, and the per-symbol detail lives in
        # bot_research_scores anyway.
        league.log_event(
            event_type="BOND_SCREENED",
            message=(
                f"Scored {score_count}/{len(SYMBOLS)} symbols. "
                + ", ".join(f"{k}={v}" for k, v in classifications.items() if v > 0)
            ),
            metadata={
                "symbols":         list(SYMBOLS),
                "score_count":     score_count,
                "classifications": classifications,
                "bars_period":     BARS_PERIOD,
            },
            run_id=run_id,
        )

        if score_count == 0:
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
                trade_count=0,           # research bot never trades
                error_count=error_count,
            )
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    status = run_cycle()
    print(f"[bond] cycle status={status}")
    sys.exit(0)
