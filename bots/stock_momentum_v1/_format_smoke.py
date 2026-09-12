"""Smoke tests for stock_momentum_v1's None-safe log formatting.

Run from the repo root:

    python -m bots.stock_momentum_v1._format_smoke

  ...or from this directory:

    python _format_smoke.py

REGRESSION UNDER TEST (2026-09-11)

    The TP_SL_HOLD log line formatted `fee_adjusted_tp`, `strength` and
    `scale` with bare `:.2%` / `:.4f` specifiers. After the 2026-08-08 ATR
    restructure all three are legitimately None when take-profit scaling is
    unavailable — which is PERMANENT for low-volatility symbols (SGOV,
    SCHD, SCHB, VTI sit below ATR_VOL_THRESHOLD essentially always).

    Formatting None with a numeric spec raises:

        TypeError: unsupported format string passed to NoneType.__format__

    raised inside the per-symbol exit loop, which has no local handler. It
    unwound to main(), so EVERY remaining symbol's stop-loss went
    unchecked and the entire entry loop was skipped — a risk-control
    outage caused by a log line.

    These tests assert the log line can no longer raise, for any
    combination of missing values.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_PASS = 0
_FAIL = 0
_SKIP = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    global _SKIP
    print("=" * 64)
    print("stock_momentum_v1 — None-safe log formatting")
    print("=" * 64)

    # bot.py imports pandas / supabase / requests at module level.
    #
    # On a developer machine missing those, a skip with guidance is more
    # useful than a red test. In CI it is the opposite: a skip would mean
    # this suite silently covers NOTHING while still reporting green, which
    # is precisely the "surface confidently reporting something untrue"
    # failure this whole test exists to catch.
    #
    # So CI sets SMOKE_STRICT=1 and an import failure becomes a hard fail.
    strict = os.getenv("SMOKE_STRICT", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    try:
        import bot  # noqa: F401
        fmt_pct, fmt_money, fmt_float = bot.fmt_pct, bot.fmt_money, bot.fmt_float
    except Exception as e:  # noqa: BLE001
        if strict:
            print(f"\n  FAIL  SMOKE_STRICT=1 and bot.py could not be imported: {e!r}")
            print("        The suite covers nothing in this state, so this is a")
            print("        hard failure rather than a skip. Check that")
            print("        agent_runner/requirements.txt installed cleanly.")
            print("\n" + "=" * 64)
            print("  0 passed, 1 failed, 0 skipped")
            print("=" * 64)
            return 1
        _SKIP += 1
        print(f"\n  SKIP  could not import bot.py: {e!r}")
        print("        Install the bot's deps into this interpreter, e.g.:")
        print("        pip install -r agent_runner/requirements.txt")
        print("        (set SMOKE_STRICT=1 to make this a hard failure)")
        print("\n" + "=" * 64)
        print(f"  {_PASS} passed, {_FAIL} failed, {_SKIP} skipped")
        print("=" * 64)
        return 0

    # ── 1. Helpers handle None ─────────────────────────────────────────────
    print("\n[1] Helpers are None-safe")
    check("fmt_pct(None) == 'N/A'", fmt_pct(None) == "N/A")
    check("fmt_money(None) == 'N/A'", fmt_money(None) == "N/A")
    check("fmt_float(None) == 'N/A'", fmt_float(None) == "N/A")
    check("fmt_pct(0.0342) == '3.42%'", fmt_pct(0.0342) == "3.42%",
          f"got {fmt_pct(0.0342)!r}")
    check("fmt_money(12.5) == '12.50'", fmt_money(12.5) == "12.50",
          f"got {fmt_money(12.5)!r}")
    check("fmt_float(1.23456) == '1.2346'", fmt_float(1.23456) == "1.2346",
          f"got {fmt_float(1.23456)!r}")
    check("negative pct", fmt_pct(-0.025) == "-2.50%", f"got {fmt_pct(-0.025)!r}")
    check("zero is not N/A", fmt_pct(0.0) == "0.00%", f"got {fmt_pct(0.0)!r}")
    check("NaN -> N/A", fmt_pct(float("nan")) == "N/A")
    check("string input -> N/A", fmt_pct("abc") == "N/A")
    check("numeric string coerces", fmt_money("12.5") == "12.50",
          f"got {fmt_money('12.5')!r}")

    # ── 2. The exact failing log line, TP unavailable ──────────────────────
    print("\n[2] TP_SL_HOLD line with TP scaling unavailable")
    pnl_pct, dynamic_sl = 0.0123, 0.025
    fee_adjusted_tp = strength = scale = None  # what the cycle produces

    # Prove the OLD form really did raise — otherwise this test proves nothing.
    old_raised = False
    try:
        _ = f"tp={fee_adjusted_tp:.2%}"
    except TypeError as e:
        old_raised = "NoneType" in str(e) or "unsupported format" in str(e)
    check("old bare format raises TypeError (regression is real)", old_raised)

    try:
        details = (
            f"pnl={fmt_pct(pnl_pct)} "
            f"between -sl={fmt_pct(-dynamic_sl if dynamic_sl is not None else None)} "
            f"and tp={fmt_pct(fee_adjusted_tp)} "
            f"(strength={fmt_float(strength)} scale={fmt_money(scale)})"
        )
        check("new form does not raise", True)
        check("tp renders N/A", "tp=N/A" in details, details)
        check("strength renders N/A", "strength=N/A" in details, details)
        check("scale renders N/A", "scale=N/A" in details, details)
        check("real values still render", "pnl=1.23%" in details and "-2.50%" in details,
              details)
        print(f"        -> {details}")
    except Exception as e:  # noqa: BLE001
        check("new form does not raise", False, repr(e))

    # ── 3. TP available: full detail preserved ─────────────────────────────
    print("\n[3] TP_SL_HOLD line with TP scaling available")
    try:
        details = (
            f"pnl={fmt_pct(0.0123)} between -sl={fmt_pct(-0.025)} "
            f"and tp={fmt_pct(0.0431)} "
            f"(strength={fmt_float(0.0087)} scale={fmt_money(1.04)})"
        )
        check("renders without N/A", "N/A" not in details, details)
        check("tp value present", "tp=4.31%" in details, details)
        print(f"        -> {details}")
    except Exception as e:  # noqa: BLE001
        check("available path does not raise", False, repr(e))

    # ── 4. Every combination of missing values ─────────────────────────────
    print("\n[4] All None combinations survive")
    ok = True
    vals = (None, 0.0, 1.5, -0.02, float("nan"), "x")
    for a in vals:
        for b in vals:
            try:
                _ = (f"pnl={fmt_pct(a)} tp={fmt_pct(b)} "
                     f"strength={fmt_float(a)} scale={fmt_money(b)}")
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"      raised on ({a!r}, {b!r}): {e!r}")
    check("36 combinations, zero raises", ok)

    print("\n" + "=" * 64)
    print(f"  {_PASS} passed, {_FAIL} failed, {_SKIP} skipped")
    print("=" * 64)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
