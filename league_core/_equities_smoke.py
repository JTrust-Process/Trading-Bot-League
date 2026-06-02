"""league_core/_equities_smoke.py — dry-run smoke tests for equities client.

Run from the repo root:

    python -m league_core._equities_smoke

Exercises the OFFLINE paths only:
  * deterministic_order_id stability and uniqueness properties
  * _build_payload shape matches the live stock bot's wire format
  * place_market_buy / place_market_sell with dry_run=True return the
    expected structured result without touching the network

The ONLINE paths (auth.get_access_token, auth.get_account_id, _post_order,
get_fill_price) are exercised when you place a real order in your first
live ETF cycle. If anything is wrong there, you'll see it in the bot_runs /
bot_errors tables for that cycle — pull the plug via LEAGUE_KILL=1 if so.
"""

from __future__ import annotations

import os
import sys

from league_core.public_api import equities


def _check(label, expected, got):
    if expected == got:
        print(f"  ok   {label}")
        return 0
    print(f"  FAIL {label}: expected {expected!r}, got {got!r}")
    return 1


def main() -> int:
    fails = 0
    print("league_core.public_api.equities — dry-run smoke tests")

    # Ensure no env-driven dry-run interferes — we pass explicit dry_run=True.
    os.environ.pop("PUBLIC_DRY_RUN", None)

    # ── deterministic_order_id ─────────────────────────────────────────────
    a = equities.deterministic_order_id("acct123", "BUY", "SPY")
    b = equities.deterministic_order_id("acct123", "BUY", "SPY")
    fails += _check("deterministic_order_id stable within same minute", a, b)

    a_sell = equities.deterministic_order_id("acct123", "SELL", "SPY")
    if a == a_sell:
        print("  FAIL different side produces SAME order_id (would dedup wrongly)")
        fails += 1
    else:
        print("  ok   different side produces different order_id")

    a_qqq = equities.deterministic_order_id("acct123", "BUY", "QQQ")
    if a == a_qqq:
        print("  FAIL different symbol produces SAME order_id")
        fails += 1
    else:
        print("  ok   different symbol produces different order_id")

    a_other = equities.deterministic_order_id("acct999", "BUY", "SPY")
    if a == a_other:
        print("  FAIL different account produces SAME order_id")
        fails += 1
    else:
        print("  ok   different account produces different order_id")

    # ── _build_payload BUY (notional) ──────────────────────────────────────
    p_buy = equities._build_payload("oid-1", "BUY", "spy", amount_usd=250.0)
    fails += _check("BUY payload orderId",      "oid-1",         p_buy["orderId"])
    fails += _check("BUY payload symbol upper", "SPY",           p_buy["instrument"]["symbol"])
    fails += _check("BUY payload type",         "EQUITY",        p_buy["instrument"]["type"])
    fails += _check("BUY payload orderSide",    "BUY",           p_buy["orderSide"])
    fails += _check("BUY payload orderType",    "MARKET",        p_buy["orderType"])
    fails += _check("BUY payload TIF",          "DAY",           p_buy["expiration"]["timeInForce"])
    fails += _check("BUY payload amount fmt",   "250.00",        p_buy["amount"])
    if "quantity" in p_buy:
        print("  FAIL BUY payload should NOT carry 'quantity'")
        fails += 1
    else:
        print("  ok   BUY payload excludes 'quantity'")

    # Rounding sanity
    p_round = equities._build_payload("oid-r", "BUY", "SPY", amount_usd=249.999)
    fails += _check("BUY payload rounding (249.999 -> 250.00)",
                    "250.00", p_round["amount"])

    # ── _build_payload SELL (quantity) ─────────────────────────────────────
    p_sell = equities._build_payload("oid-2", "SELL", "QQQ", quantity=0.123456789)
    fails += _check("SELL payload orderSide",    "SELL",        p_sell["orderSide"])
    fails += _check("SELL payload quantity fmt", "0.12345679",  p_sell["quantity"])
    if "amount" in p_sell:
        print("  FAIL SELL payload should NOT carry 'amount'")
        fails += 1
    else:
        print("  ok   SELL payload excludes 'amount'")

    # ── place_market_buy dry_run ───────────────────────────────────────────
    # auth.get_account_id is called inside place_market_buy even in dry-run.
    # In a fresh test environment PUBLIC_ACCOUNT_ID may not be set; we set
    # a dummy one to keep the dry-run path self-contained.
    os.environ["PUBLIC_ACCOUNT_ID"] = "test-account-id"
    from league_core.public_api import auth
    auth.reset_caches()

    res = equities.place_market_buy("SPY", 250.0, dry_run=True)
    fails += _check("BUY dry_run ok=True",         True,   res["ok"])
    fails += _check("BUY dry_run response.dry_run", True,  res["response"]["dry_run"])
    fails += _check("BUY dry_run payload symbol",   "SPY", res["payload"]["instrument"]["symbol"])
    fails += _check("BUY dry_run payload amount",   "250.00", res["payload"]["amount"])
    if not res["order_id"]:
        print("  FAIL BUY dry_run missing order_id")
        fails += 1
    else:
        print("  ok   BUY dry_run carries order_id")

    # Invalid inputs
    res_zero = equities.place_market_buy("SPY", 0.0, dry_run=True)
    fails += _check("BUY amount=0 refused", False, res_zero["ok"])
    fails += _check("BUY amount=0 reason",  "amount_usd_non_positive", res_zero["error"])

    res_empty = equities.place_market_buy("", 100.0, dry_run=True)
    fails += _check("BUY symbol='' refused", False, res_empty["ok"])
    fails += _check("BUY symbol='' reason",  "symbol_empty", res_empty["error"])

    # ── place_market_sell dry_run ──────────────────────────────────────────
    res_s = equities.place_market_sell("QQQ", 0.5, dry_run=True)
    fails += _check("SELL dry_run ok=True", True, res_s["ok"])
    fails += _check("SELL dry_run side",    "SELL", res_s["payload"]["orderSide"])
    fails += _check("SELL dry_run quantity", "0.50000000", res_s["payload"]["quantity"])

    res_s_zero = equities.place_market_sell("QQQ", 0, dry_run=True)
    fails += _check("SELL quantity=0 refused", False, res_s_zero["ok"])

    # ── PUBLIC_DRY_RUN env opt-in ─────────────────────────────────────────
    os.environ["PUBLIC_DRY_RUN"] = "1"
    res_env = equities.place_market_buy("SPY", 100.0)  # no explicit dry_run
    fails += _check("env PUBLIC_DRY_RUN=1 triggers dry_run",
                    True, res_env["response"]["dry_run"])
    os.environ.pop("PUBLIC_DRY_RUN", None)

    # ── Done ───────────────────────────────────────────────────────────────
    print("=" * 50)
    if fails:
        print(f"FAILED {fails} case(s)")
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
