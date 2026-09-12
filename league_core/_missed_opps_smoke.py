"""Smoke tests for league_core.missed_opps — Phase 1 missed-opportunity tracking.

Run from the repo root:

    python -m league_core._missed_opps_smoke

No network, no Supabase, no API key, no AI. Pure in-process assertions that
the safety properties hold. Every test here is about one question:

    "Can this module affect the trading bot?"

The answer must be no under every condition tested, including a badly
configured Supabase, a hostile input, and a total import failure.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from league_core import missed_opps as mo  # noqa: E402


_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# Precision the implementation stores. _forward_returns() rounds to 6 decimal
# places, so 106/105-1 = 0.009523809523... is stored as 0.009524.
_RETURN_PLACES = 6


def approx(got, want: float, *, places: int = _RETURN_PLACES, tol: float = 1e-12) -> bool:
    """True when `got` equals `want` rounded to the stored precision.

    WHY THIS EXISTS (bug fixed 2026-09-11): the original assertions compared
    against the UNROUNDED quotient with a 1e-9 tolerance. Rounding to 6 dp
    introduces up to 5e-7 of error — roughly 500x the tolerance — so three
    assertions failed while the implementation was returning mathematically
    correct values. The test was wrong, not the maths.

    Comparing against `round(want, places)` asserts the actual contract
    ("decimal returns at 6 dp") rather than a precision the code never
    claimed to provide. A wrong test that fails is cheap; a wrong test that
    gets "fixed" by loosening the implementation is how real precision bugs
    get introduced.
    """
    if got is None:
        return False
    try:
        return abs(float(got) - round(float(want), places)) <= tol
    except BaseException:
        return False


def _clear_env() -> None:
    for k in ("MISSED_OPP_TRACKING", "LEAGUE_SUPABASE_URL",
              "LEAGUE_SUPABASE_KEY", "LEAGUE_BOT_ID"):
        os.environ.pop(k, None)


# ── 1. The flag defaults OFF ────────────────────────────────────────────────

def test_default_off() -> None:
    print("\n[1] MISSED_OPP_TRACKING defaults OFF")
    _clear_env()
    check("unset -> tracking_enabled() False", mo.tracking_enabled() is False)

    os.environ["MISSED_OPP_TRACKING"] = "0"
    check("'0' -> False", mo.tracking_enabled() is False)

    for val in ("", "false", "no", "off", "FALSE", "banana", "2", " "):
        os.environ["MISSED_OPP_TRACKING"] = val
        check(f"{val!r} -> False", mo.tracking_enabled() is False)

    for val in ("1", "true", "TRUE", "yes", "on", " 1 "):
        os.environ["MISSED_OPP_TRACKING"] = val
        check(f"{val!r} -> True", mo.tracking_enabled() is True)
    _clear_env()


# ── 2. No insert attempted while disabled ───────────────────────────────────

def test_no_insert_when_disabled() -> None:
    print("\n[2] Disabled -> no network call at all")
    _clear_env()
    # Config IS present; only the flag is missing. Proves the flag gates
    # ahead of config, so an operator who hasn't opted in sends nothing.
    os.environ["LEAGUE_SUPABASE_URL"] = "https://example.invalid"
    os.environ["LEAGUE_SUPABASE_KEY"] = "fake-key"

    calls: list = []
    real_post = getattr(mo.requests, "post", None)
    if real_post is not None:
        mo.requests.post = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
            AssertionError("network call made while tracking disabled")
        )
    try:
        r = mo.safe_insert_missed_opportunity(symbol="AAPL", skip_reason="test")
        check("returns False when disabled", r is False)
        check("zero network calls", len(calls) == 0, f"got {len(calls)}")

        os.environ["MISSED_OPP_TRACKING"] = "0"
        r = mo.safe_insert_missed_opportunity(symbol="AAPL", skip_reason="test")
        check("'0' returns False", r is False)
        check("still zero network calls", len(calls) == 0)

        stats = mo.update_missed_opportunity_outcomes()
        check("scorer no-ops when disabled", stats["considered"] == 0)
    finally:
        if real_post is not None:
            mo.requests.post = real_post
        _clear_env()


# ── 3. Enabled + broken config -> no crash ──────────────────────────────────

def test_bad_config_no_crash() -> None:
    print("\n[3] Enabled with bad/missing Supabase config -> no crash")
    _clear_env()
    os.environ["MISSED_OPP_TRACKING"] = "1"

    r = mo.safe_insert_missed_opportunity(symbol="AAPL", skip_reason="no config")
    check("missing config returns False, no raise", r is False)
    stats = mo.update_missed_opportunity_outcomes()
    check("scorer survives missing config", isinstance(stats, dict))

    # Unroutable host: exercises the real exception path in requests.
    os.environ["LEAGUE_SUPABASE_URL"] = "http://127.0.0.1:1"
    os.environ["LEAGUE_SUPABASE_KEY"] = "fake-key"
    r = mo.safe_insert_missed_opportunity(
        symbol="AAPL", skip_reason="unreachable host", timeout=0.4,
    )
    check("connection failure returns False, no raise", r is False)
    stats = mo.update_missed_opportunity_outcomes(limit=1, timeout=0.4)
    check("scorer survives connection failure", stats["errors"] >= 0)
    _clear_env()


# ── 4. Hostile / malformed inputs ───────────────────────────────────────────

def test_hostile_inputs() -> None:
    print("\n[4] Malformed inputs never raise")
    _clear_env()
    os.environ["MISSED_OPP_TRACKING"] = "1"
    os.environ["LEAGUE_SUPABASE_URL"] = "http://127.0.0.1:1"
    os.environ["LEAGUE_SUPABASE_KEY"] = "k"

    cases = [
        {"symbol": "", "skip_reason": "empty symbol"},
        {"symbol": "AAPL", "skip_reason": ""},
        {"symbol": "A" * 500, "skip_reason": "x" * 5000},
        {"symbol": "AAPL", "skip_reason": "nan", "score": float("nan")},
        {"symbol": "AAPL", "skip_reason": "inf", "score": float("inf")},
        {"symbol": "AAPL", "skip_reason": "bad rank", "rank": "not-an-int"},
        {"symbol": "AAPL", "skip_reason": "none price", "price": None},
    ]
    ok = True
    for c in cases:
        try:
            mo.safe_insert_missed_opportunity(timeout=0.3, **c)  # type: ignore[arg-type]
        except BaseException as e:  # noqa: BLE001
            ok = False
            print(f"      raised on {c}: {e!r}")
    check("no input raises", ok)

    check("NaN dropped", mo._to_float(float("nan")) is None)
    check("inf dropped", mo._to_float(float("inf")) is None)
    check("-999.0 sentinel kept", mo._to_float(-999.0) == -999.0)
    check("int rank coerced", mo._to_int("4") == 4)
    check("bad rank -> None", mo._to_int("x") is None)
    _clear_env()


# ── 5. Skip-reason bucketing ────────────────────────────────────────────────

def test_classify() -> None:
    print("\n[5] Skip reasons bucket correctly")
    cases = {
        "no momentum or breakout signal": "no_signal",
        "momentum rank=4 score=0.0123, no breakout": "no_signal",
        "In cooldown, skipping buy": "cooldown",
        "Open Supabase position exists (entry=2026-09-01); skipping buy": "already_held",
        "no price data or insufficient bars (got 0)": "insufficient_data",
        "invalid price or ATR": "bad_data",
        "alloc $3.80 and budget $4.10 both below $5.00 broker minimum": "below_min_order",
        "": "other",
    }
    for reason, want in cases.items():
        got = mo.classify_skip_reason(reason)
        check(f"{reason[:44]!r} -> {want}", got == want, f"got {got}")
    check("None input safe", mo.classify_skip_reason(None) == "other")  # type: ignore[arg-type]


# ── 6. Forward-return maths ─────────────────────────────────────────────────

def test_forward_returns() -> None:
    print("\n[6] Forward returns")
    bars = [{"timestamp": f"2026-09-{d:02d}T00:00:00Z", "close": 100.0 + d}
            for d in range(1, 28)]

    rets, measured = mo._forward_returns(bars, "2026-09-05", None)
    check("measured", measured is True)

    # Baseline = close of the last bar on or before 2026-09-05 = 105.0.
    # Returns are DECIMAL (0.009524 == +0.9524%), not percent, stored at 6 dp.
    check("1d = 106/105-1 (decimal, 6dp)", approx(rets[1], 106 / 105 - 1),
          f"got {rets[1]!r} want {round(106 / 105 - 1, 6)!r}")
    check("5d = 110/105-1 (decimal, 6dp)", approx(rets[5], 110 / 105 - 1),
          f"got {rets[5]!r} want {round(110 / 105 - 1, 6)!r}")
    check("20d = 125/105-1 (decimal, 6dp)", approx(rets[20], 125 / 105 - 1),
          f"got {rets[20]!r} want {round(125 / 105 - 1, 6)!r}")

    # Explicit guard against a future "helpful" conversion to percent.
    # +0.95% must be ~0.0095, never ~0.95.
    check("returns are decimal, not percent", 0.0 < rets[1] < 0.01,
          f"got {rets[1]!r} — looks like percent if >= 0.01")

    # A flat series must produce exactly zero, not float noise.
    flat = [{"timestamp": f"2026-09-{d:02d}T00:00:00Z", "close": 50.0}
            for d in range(1, 28)]
    flat_rets, _ = mo._forward_returns(flat, "2026-09-05", None)
    check("flat series -> 0.0 exactly", flat_rets[1] == 0.0, f"got {flat_rets[1]!r}")

    # A decline must be negative.
    down = [{"timestamp": f"2026-09-{d:02d}T00:00:00Z", "close": 200.0 - d}
            for d in range(1, 28)]
    down_rets, _ = mo._forward_returns(down, "2026-09-05", None)
    check("decline -> negative return", down_rets[1] is not None and down_rets[1] < 0,
          f"got {down_rets[1]!r}")

    # Near the end of history: 1d measurable, 20d not.
    rets2, measured2 = mo._forward_returns(bars, "2026-09-26", None)
    check("partial horizons still measured", measured2 is True)
    check("20d None when data short", rets2[20] is None)

    # Skip date before all history -> nothing measurable.
    rets3, measured3 = mo._forward_returns(bars, "2026-08-01", None)
    check("pre-history not measured", measured3 is False)
    check("empty bars safe", mo._forward_returns([], "2026-09-05", None)[1] is False)
    check("garbage bars safe",
          mo._forward_returns([{"nope": 1}], "2026-09-05", None)[1] is False)


# ── 7. The bot-side hook contract ───────────────────────────────────────────

def test_hook_contract() -> None:
    print("\n[7] bot hook returns None and never raises")
    _clear_env()
    hook_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bots", "stock_momentum_v1",
    )
    sys.path.insert(0, hook_dir)
    try:
        from missed_opps_hook import record_skipped_candidate  # noqa: E402

        check("returns None when disabled",
              record_skipped_candidate("AAPL", "reason") is None)

        os.environ["MISSED_OPP_TRACKING"] = "1"
        os.environ["LEAGUE_SUPABASE_URL"] = "http://127.0.0.1:1"
        os.environ["LEAGUE_SUPABASE_KEY"] = "k"
        check("returns None on connection failure",
              record_skipped_candidate("AAPL", "reason", score=0.01, rank=4,
                                       regime="bull") is None)
        check("returns None on hostile input",
              record_skipped_candidate(None, None, score="x") is None)  # type: ignore[arg-type]
    except BaseException as e:  # noqa: BLE001
        check("hook import/behaviour", False, repr(e))
    finally:
        _clear_env()


def main() -> int:
    print("=" * 62)
    print("missed_opps smoke tests — no network, no AI, no API key")
    print("=" * 62)
    test_default_off()
    test_no_insert_when_disabled()
    test_bad_config_no_crash()
    test_hostile_inputs()
    test_classify()
    test_forward_returns()
    test_hook_contract()
    print("\n" + "=" * 62)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 62)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
