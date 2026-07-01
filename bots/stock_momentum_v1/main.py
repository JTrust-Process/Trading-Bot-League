"""bots/stock_momentum_v1/main.py — agent_runner entry point.

Thin wrapper around the vendored stock bot's `bot.py`. agent_runner's
scheduler imports this module and calls `run_cycle()` the same way it
does for every other bot in `bots/`.

This bot was vendored into the League repo from
`C:\\Users\\Jeremiah\\source\\Trading Bot\\Trading Bot Project\\` on
2026-06-01 as part of the GHA → Fly migration. The vendored files use
a flat layout (e.g. `from monitor import Monitor`, not a package), so
we add this directory to sys.path before importing `bot`. The vendored
code itself is intentionally NOT modified by this wrapper — it should
remain byte-identical to the original so we can spot drift later via
diff. All changes go through this wrapper or through follow-on PRs that
explicitly say what they touch in the bot itself.

History / future work:
  * Stage now: code lives here, bot still runs on GHA in the original
    repo. Fly scheduling for this bot isn't wired yet — that's the next
    PR (add a job to `agent_runner/scheduler.py`).
  * Eventually: integrate `league_core.risk.preflight()` at the order-
    placement entry points in `bot.py` so the League risk gate fires
    alongside the bot's own internal risk checks. Both must pass.
"""

from __future__ import annotations

import os
import sys
import traceback

# Make this directory importable so the vendored bot code, which uses
# flat-layout imports like `from monitor import Monitor`, can find its
# siblings. MUST run before `import bot` below.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def run_cycle() -> int:
    """Run one cycle of the stock momentum bot. Returns a status int:
        0 = normal completion (success or warning)
        1 = uncaught exception (logged here for visibility)

    APScheduler in agent_runner.scheduler uses the return value only for
    its own log line — the real status is what the bot writes to
    `bot_runs` (its own Supabase project) and to the League's `bot_runs`
    via `league_status.end_run`. We mirror the existing GHA contract
    where `python bot.py` is expected to exit 0 even on internal
    warnings, with state recorded in Supabase rather than the exit code.
    """
    # Lazy import: `bot` pulls in pandas, the Supabase client, the Public
    # API client, etc. — heavy. Importing only when run_cycle is actually
    # called keeps `python -m bots.stock_momentum_v1` lightweight for
    # health checks and dry imports.
    try:
        import bot  # noqa: WPS433 - intentional inside-function import
    except Exception as e:  # noqa: BLE001
        print(f"[stock_momentum_v1] import bot failed: {e!r}")
        traceback.print_exc()
        return 1

    try:
        bot.main()
        return 0
    except SystemExit as e:
        # bot.main() may call sys.exit() in its own finally — treat any
        # exit-zero as success, anything else as failure.
        code = e.code if isinstance(e.code, int) else 0
        return 0 if code == 0 else 1
    except Exception as e:  # noqa: BLE001
        # The bot's own try/finally inside main() handles state cleanup
        # (Monitor.end_run, league_status.end_run). We just surface the
        # failure to APScheduler so it shows up in fly logs.
        print(f"[stock_momentum_v1] cycle crashed: {e!r}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_cycle())
