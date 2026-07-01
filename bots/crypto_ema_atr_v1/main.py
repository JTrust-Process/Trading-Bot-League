"""bots/crypto_ema_atr_v1/main.py — agent_runner entry point.

Thin wrapper around the vendored crypto bot's entry script (`_entry.py`,
originally `main.py` in the source repo — renamed on vendor to avoid
colliding with this wrapper). agent_runner's scheduler imports this
module and calls `run_cycle()` the same way it does for every other
bot in `bots/`.

The vendored crypto entry is script-style — all its logic lives inside
its `if __name__ == "__main__":` block, with no reusable `def main()`.
Rather than modify the vendored code to expose a callable, this wrapper
uses `runpy.run_module(..., run_name="__main__")` to execute it as if
it were being run as a script. The vendored code stays byte-identical
to the original so we can diff-check for drift later.

The vendored `crypto_bot/` package is at the same directory level as
`_entry.py` so its imports (`from crypto_bot.core.engine import run`,
etc.) resolve without any refactor — we just add this directory to
sys.path.

History: vendored from
`C:\\Users\\Jeremiah\\source\\Crypto_Trading_Project\\Crypto_Trading_Bot\\`
on 2026-06-01 as part of the GHA → Fly migration. See README.md.
"""

from __future__ import annotations

import os
import runpy
import sys
import traceback

# Add this directory to sys.path so BOTH `_entry` (the renamed original
# main.py) AND the `crypto_bot` package resolve as top-level imports.
# MUST run before runpy.run_module below.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def run_cycle() -> int:
    """Run one cycle of the crypto EMA+ATR bot. Returns:
        0 = normal completion (or a caller-friendly SystemExit(0))
        1 = uncaught exception (logged here for visibility)

    Executes the vendored entry script via runpy — its
    `if __name__ == "__main__":` block runs top-to-bottom, calling
    `crypto_bot.core.engine.run(monitor, run_id)` inside a try/finally
    that handles Monitor + league_status cleanup on its own.
    """
    try:
        runpy.run_module("_entry", run_name="__main__")
        return 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
        return 0 if code == 0 else 1
    except Exception as e:  # noqa: BLE001
        # The vendored code's try/finally already handles Monitor +
        # league_status cleanup before the exception reaches us. We just
        # surface the failure to APScheduler's log line and return
        # non-zero so the run is visibly failed.
        print(f"[crypto_ema_atr_v1] cycle crashed: {e!r}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_cycle())
