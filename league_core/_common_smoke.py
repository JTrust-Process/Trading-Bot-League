"""league_core/_common_smoke.py — offline tests for the shared helpers.

Run from the repo root:

    python -m league_core._common_smoke

Fully offline. Covers the env parsers, the canonical asset-class
classifier, and the series maths — the functions that were duplicated
across bots before the 2026-07-25 consolidation.

The classifier cases matter most: divergent per-bot ETF allowlists meant
the same ticker was written to bot_trades.asset_class differently
depending on which bot logged it.
"""

from __future__ import annotations

import os
import sys

from league_core import common


def _check(label, expected, got):
    if expected == got:
        print(f"  ok   {label}")
        return 0
    print(f"  FAIL {label}: expected {expected!r}, got {got!r}")
    return 1


def main() -> int:
    fails = 0
    print("league_core.common — offline smoke tests")

    # ── env_float ──────────────────────────────────────────────────────────
    os.environ.pop("SMOKE_F", None)
    fails += _check("env_float unset -> default", 1.5, common.env_float("SMOKE_F", 1.5))
    os.environ["SMOKE_F"] = ""
    fails += _check("env_float blank -> default", 1.5, common.env_float("SMOKE_F", 1.5))
    os.environ["SMOKE_F"] = "2.25"
    fails += _check("env_float parses", 2.25, common.env_float("SMOKE_F", 1.5))
    os.environ["SMOKE_F"] = "abc"
    fails += _check("env_float garbage -> default", 1.5, common.env_float("SMOKE_F", 1.5))
    os.environ.pop("SMOKE_F", None)

    # ── env_int ────────────────────────────────────────────────────────────
    os.environ.pop("SMOKE_I", None)
    fails += _check("env_int unset -> default", 200, common.env_int("SMOKE_I", 200))
    os.environ["SMOKE_I"] = "50"
    fails += _check("env_int parses", 50, common.env_int("SMOKE_I", 200))
    # The case the old int(os.getenv(...)) form got wrong.
    os.environ["SMOKE_I"] = "50.0"
    fails += _check("env_int tolerates float-ish '50.0'", 50, common.env_int("SMOKE_I", 200))
    os.environ["SMOKE_I"] = "nope"
    fails += _check("env_int garbage -> default", 200, common.env_int("SMOKE_I", 200))
    os.environ.pop("SMOKE_I", None)

    # ── env_bool ───────────────────────────────────────────────────────────
    os.environ.pop("SMOKE_B", None)
    fails += _check("env_bool unset -> default True", True, common.env_bool("SMOKE_B", True))
    fails += _check("env_bool unset -> default False", False, common.env_bool("SMOKE_B", False))
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        os.environ["SMOKE_B"] = truthy
        fails += _check(f"env_bool {truthy!r} truthy", True, common.env_bool("SMOKE_B", False))
    for falsy in ("0", "false", "no", "off"):
        os.environ["SMOKE_B"] = falsy
        fails += _check(f"env_bool {falsy!r} falsy", False, common.env_bool("SMOKE_B", True))
    # A typo must not silently read as true.
    os.environ["SMOKE_B"] = "ture"
    fails += _check("env_bool typo -> False", False, common.env_bool("SMOKE_B", True))
    os.environ.pop("SMOKE_B", None)

    # ── env_list ───────────────────────────────────────────────────────────
    os.environ.pop("SMOKE_L", None)
    fails += _check("env_list unset -> default", ("BTC",), common.env_list("SMOKE_L", ["BTC"]))
    fails += _check("env_list unset, no default -> ()", (), common.env_list("SMOKE_L"))
    os.environ["SMOKE_L"] = " spy , qqq ,, vti "
    fails += _check("env_list trims/uppers/drops blanks",
                    ("SPY", "QQQ", "VTI"), common.env_list("SMOKE_L"))
    os.environ.pop("SMOKE_L", None)

    # ── classify_asset_class ───────────────────────────────────────────────
    # The three symbols that exposed the divergence.
    fails += _check("XLK -> etf (was equity in options_alert)",
                    "etf", common.classify_asset_class("XLK"))
    fails += _check("SGOV -> etf (ETF bot's bear basket)",
                    "etf", common.classify_asset_class("SGOV"))
    fails += _check("SCHD -> etf", "etf", common.classify_asset_class("SCHD"))
    fails += _check("SPY -> etf", "etf", common.classify_asset_class("SPY"))
    fails += _check("AAPL -> equity", "equity", common.classify_asset_class("AAPL"))
    fails += _check("TSLA -> equity", "equity", common.classify_asset_class("TSLA"))
    fails += _check("lower-case spy -> etf", "etf", common.classify_asset_class("spy"))
    fails += _check("whitespace ' spy ' -> etf", "etf", common.classify_asset_class(" spy "))
    fails += _check("empty -> equity", "equity", common.classify_asset_class(""))
    fails += _check("None -> equity", "equity", common.classify_asset_class(None))

    # ── closes ─────────────────────────────────────────────────────────────
    bars = [{"close": "1.0"}, {"close": 2}, {"nope": 3}, {"close": "bad"}, {"close": 4.5}]
    fails += _check("closes skips malformed rows", [1.0, 2.0, 4.5], common.closes(bars))
    fails += _check("closes handles empty", [], common.closes([]))
    fails += _check("closes handles None", [], common.closes(None))

    # ── sma / rolling / pct_return ─────────────────────────────────────────
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    fails += _check("sma period 5", 3.0, common.sma(vals, 5))
    fails += _check("sma period 2 uses tail", 4.5, common.sma(vals, 2))
    fails += _check("sma too short -> None", None, common.sma(vals, 6))
    fails += _check("sma period 0 -> None", None, common.sma(vals, 0))
    fails += _check("rolling_low", 4.0, common.rolling_low(vals, 2))
    fails += _check("rolling_high", 5.0, common.rolling_high(vals, 2))
    fails += _check("pct_return 4 back", 4.0, common.pct_return(vals, 4))
    fails += _check("pct_return too short -> None", None, common.pct_return(vals, 5))
    fails += _check("pct_return non-positive start -> None",
                    None, common.pct_return([0.0, 1.0], 1))

    print("=" * 50)
    if fails:
        print(f"FAILED {fails} case(s)")
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
