"""league_core/_risk_smoke.py — assert-based smoke tests for risk._evaluate_rules.

Run from the repo root:

    python -m league_core._risk_smoke

Hits the PURE rules engine only — no Supabase, no network. The integration
side (config, registry fetch, trade-count fetch) is intentionally not
tested here because that would require a live League project; you exercise
it on first deploy by watching the next bot cycle.

Every test below has the form:

    registry = {...}                                # registry row
    ok, reason = risk._evaluate_rules(...)
    assert (ok, reason) == EXPECTED

If any assert trips, stop and figure out why before deploying.
"""

from __future__ import annotations

import sys

from league_core import risk


def _live_etf_registry(**overrides):
    """A reasonable 'live ETF rotation' registry row for happy-path tests."""
    base = {
        "bot_id":               "etf_rotation_v1",
        "bot_type":             "etf",
        "mode":                 "live",
        "status":               "enabled",
        "allowed_instruments":  ["SPY", "QQQ", "VTI", "SCHD", "SGOV"],
        "can_place_orders":     True,
        "manual_approval_required": False,
        "max_order_usd":        50.0,
        "max_daily_trades":     10,
        "max_open_positions":   5,
    }
    base.update(overrides)
    return base


def _check(label, ok_expected, reason_expected, ok_got, reason_got):
    if (ok_got, reason_got) != (ok_expected, reason_expected):
        print(f"  FAIL {label}: expected ({ok_expected}, {reason_expected!r}) "
              f"got ({ok_got}, {reason_got!r})")
        return 1
    print(f"  ok   {label}")
    return 0


def main() -> int:
    fails = 0
    print("league_core.risk._evaluate_rules — smoke tests")

    # ── Happy path ──────────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=50.0,
        daily_trade_count=3,
    )
    fails += _check("happy path BUY SPY $50",
                    True, risk.REASON_OK, ok, r)

    # ── Global kill switch via env ──────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=50.0,
        kill_env="1",
    )
    fails += _check("LEAGUE_KILL=1 refuses",
                    False, risk.REASON_KILL_GLOBAL, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=50.0,
        kill_env="true",
    )
    fails += _check("LEAGUE_KILL=true refuses (truthy variants)",
                    False, risk.REASON_KILL_GLOBAL, ok, r)

    # ── Per-bot kill ────────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(status="disabled"),
        action="BUY", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("bot status=disabled refuses",
                    False, risk.REASON_KILL_BOT, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(status="killed"),
        action="BUY", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("bot status=killed refuses",
                    False, risk.REASON_KILL_BOT, ok, r)

    # ── Mode must be live ───────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(mode="paper"),
        action="BUY", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("mode=paper refuses",
                    False, risk.REASON_MODE_NOT_LIVE, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(mode="research"),
        action="BUY", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("mode=research refuses",
                    False, risk.REASON_MODE_NOT_LIVE, ok, r)

    # ── agent_research blocked unconditionally ─────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(bot_type="agent_research"),
        action="BUY", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("bot_type=agent_research refuses (even when 'live')",
                    False, risk.REASON_AGENT_RESEARCH, ok, r)

    # ── Action validity ─────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="LIQUIDATE", symbol="SPY", amount_usd=50.0,
    )
    fails += _check("unknown action refuses",
                    False, risk.REASON_INVALID_ACTION, ok, r)

    # ── Symbol allowlist ────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="TSLA", amount_usd=50.0,
    )
    fails += _check("symbol not in allowlist refuses",
                    False, risk.REASON_SYMBOL_NOT_ALLOWED, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(allowed_instruments=["*"]),
        action="BUY", symbol="TSLA", amount_usd=50.0,
        daily_trade_count=0,
    )
    fails += _check("['*'] in allowlist = any symbol",
                    True, risk.REASON_OK, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(allowed_instruments=[]),
        action="BUY", symbol="TSLA", amount_usd=50.0,
        daily_trade_count=0,
    )
    fails += _check("empty allowlist = any symbol (convention)",
                    True, risk.REASON_OK, ok, r)

    # Case-insensitive symbol match
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="spy", amount_usd=50.0,
        daily_trade_count=0,
    )
    fails += _check("lower-case symbol matches uppercase allowlist",
                    True, risk.REASON_OK, ok, r)

    # ── max_order_usd ──────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=51.0,
    )
    fails += _check("amount > max_order_usd refuses",
                    False, risk.REASON_OVER_MAX_ORDER, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=50.0,
        daily_trade_count=0,
    )
    fails += _check("amount == max_order_usd allowed (boundary)",
                    True, risk.REASON_OK, ok, r)

    # ── Daily trade cap ─────────────────────────────────────────────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="BUY", symbol="SPY", amount_usd=50.0,
        daily_trade_count=10,
    )
    fails += _check("daily_trade_count == cap refuses BUY",
                    False, risk.REASON_DAILY_TRADES_CAP, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="SELL", symbol="SPY", amount_usd=50.0,
        daily_trade_count=999,
    )
    fails += _check("daily_trade_count >> cap STILL ALLOWS SELL (close)",
                    True, risk.REASON_OK, ok, r)

    ok, r = risk._evaluate_rules(
        _live_etf_registry(max_daily_trades=0),
        action="BUY", symbol="SPY", amount_usd=50.0,
        daily_trade_count=999,
    )
    fails += _check("max_daily_trades=0 means 'no cap' (no refusal)",
                    True, risk.REASON_OK, ok, r)

    # ── Closes bypass trade-cap but still subject to max_order ─────────────
    ok, r = risk._evaluate_rules(
        _live_etf_registry(),
        action="SELL", symbol="SPY", amount_usd=51.0,
        daily_trade_count=0,
    )
    fails += _check("SELL still subject to max_order_usd",
                    False, risk.REASON_OVER_MAX_ORDER, ok, r)

    # ── agent_research check happens BEFORE allowlist (defense in depth) ───
    ok, r = risk._evaluate_rules(
        _live_etf_registry(bot_type="agent_research", allowed_instruments=["*"]),
        action="BUY", symbol="SPY", amount_usd=50.0,
        daily_trade_count=0,
    )
    fails += _check("agent_research refused even with wildcard allowlist",
                    False, risk.REASON_AGENT_RESEARCH, ok, r)

    # ── Done ────────────────────────────────────────────────────────────────
    print("=" * 50)
    if fails:
        print(f"FAILED {fails} case(s)")
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
