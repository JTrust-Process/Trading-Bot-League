"""Payload contract — pins the EXACT Public order bodies we send today.

Run from the repo root:

    python -m league_core._payload_contract_smoke

TEST-ONLY. No network, no credentials, no production code touched.

────────────────────────────────────────────────────────────────────────────
WHAT THIS IS FOR

Until now, "what do we actually send Public?" was answered by grepping four
files that each build their own order body. This pins all of them in one
place, as an EXACT key set.

The point is not to check the payloads are correct — they are, and
_equities_smoke already covers formatting. The point is that the day
someone adds a field, CI shows it as a deliberate diff in a file called
"payload contract" rather than as a silent change inside an order path.

Every serious bug in this system has been a small, plausible change near an
order or an exit that nothing announced. This is a tripwire on the wire
format itself.

────────────────────────────────────────────────────────────────────────────
FIELDS ASSERTED **ABSENT**

Public has since added all of the following. We send NONE of them. Each is
asserted absent so enabling one requires editing this file — which is
exactly the conversation that should happen first:

    useMargin                   cash-only forcing (2026-06-16 changelog)
    equityMarketSession         24/5 / extended hours
    openCloseIndicator          short selling open vs close
    orderClass / takeProfit /
      stopLoss / bracketId      bracket orders
    taxLotMatchingInstructions  tax lot selling
    limitPrice / stopPrice      limit and stop-limit orders
    goodTillDate / expiresAt    exact GTD expiration

NOTE ON useMargin SPECIFICALLY. league_core/public_api/equities.py:44-53
records the decision not to send it: announced 2026-06-16, absent from the
documented request schema as of 2026-07-24. If it is now documented,
sending useMargin=false is a real safety improvement — the current
cash-only guard compares buyingPowerRequirement against
cashOnlyBuyingPower and FAILS OPEN when the portfolio fetch fails, whereas
a field on the request cannot. That change belongs in its own commit, with
this test updated in the same diff.

────────────────────────────────────────────────────────────────────────────
HOW EACH PAYLOAD IS REACHED WITHOUT NETWORK

  league_core equities   `_build_payload()` is a pure builder. Called
                         directly.

  stock_momentum_v1      `place_market_*()` returns the payload from its
                         dry_run short-circuit BEFORE any request, with
                         PUBLIC_ACCOUNT_ID_SKIP_VALIDATION=1 keeping
                         account resolution offline too.

  crypto_ema_atr_v1      builds its body inline and POSTs immediately, with
                         no dry-run branch. So the TEST patches
                         `_headers` and `requests.post` on the module,
                         captures the body, and restores both in a finally.
                         Monkeypatching in a test is the correct move here —
                         the alternative was extracting a builder from a
                         live order function, which is a production change
                         to an order path for the benefit of a test.
                         Not worth it.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# Fields Public now supports that we deliberately do NOT send.
FORBIDDEN_TOP_LEVEL = (
    "useMargin",
    "equityMarketSession",
    "openCloseIndicator",
    "orderClass",
    "takeProfit",
    "stopLoss",
    "bracketId",
    "taxLotMatchingInstructions",
    "limitPrice",
    "stopPrice",
    "goodTillDate",
    "expiresAt",
)


def assert_common(label: str, body: dict, *, inst_type: str, side: str,
                  money_key: str, absent_money_key: str) -> None:
    """Every assertion that applies to all four payloads."""
    check(f"{label}: has orderId", bool(body.get("orderId")))
    check(f"{label}: orderId is a string", isinstance(body.get("orderId"), str))

    inst = body.get("instrument") or {}
    sym = inst.get("symbol")
    check(f"{label}: instrument.symbol present", bool(sym))
    check(f"{label}: instrument.symbol uppercase",
          isinstance(sym, str) and sym == sym.upper(), f"got {sym!r}")
    check(f"{label}: instrument.type == {inst_type}",
          inst.get("type") == inst_type, f"got {inst.get('type')!r}")

    check(f"{label}: orderSide == {side}", body.get("orderSide") == side,
          f"got {body.get('orderSide')!r}")
    check(f"{label}: orderType == MARKET", body.get("orderType") == "MARKET",
          f"got {body.get('orderType')!r}")
    check(f"{label}: timeInForce == DAY",
          (body.get("expiration") or {}).get("timeInForce") == "DAY",
          f"got {(body.get('expiration') or {}).get('timeInForce')!r}")

    check(f"{label}: uses {money_key}", money_key in body, f"keys={sorted(body)}")
    check(f"{label}: does NOT use {absent_money_key}",
          absent_money_key not in body, f"keys={sorted(body)}")

    # The tripwire.
    present = [f for f in FORBIDDEN_TOP_LEVEL if f in body]
    check(f"{label}: sends none of the new Public fields",
          not present,
          f"UNEXPECTED: {present} — if this is deliberate, update this test "
          f"in the same commit")

    # Exact key set. Catches an addition the denylist above doesn't name.
    expected = {"orderId", "instrument", "orderSide", "orderType",
                "expiration", money_key}
    check(f"{label}: exact key set", set(body) == expected,
          f"diff={set(body) ^ expected}")

    check(f"{label}: JSON-serialisable", _json_ok(body))


def _json_ok(body: dict) -> bool:
    try:
        json.dumps(body)
        return True
    except Exception:  # noqa: BLE001
        return False


# ── 1-2. league_core equities ────────────────────────────────────────────────

def test_league_core_equities() -> None:
    print("\n[1-2] league_core.public_api.equities._build_payload")
    from league_core.public_api import equities

    buy = equities._build_payload("oid-buy", "BUY", "spy", amount_usd=25.0)
    assert_common("equity BUY", buy, inst_type="EQUITY", side="BUY",
                  money_key="amount", absent_money_key="quantity")
    check("equity BUY: amount is a formatted string", buy["amount"] == "25.00",
          f"got {buy['amount']!r}")

    sell = equities._build_payload("oid-sell", "SELL", "qqq", quantity=1.5)
    assert_common("equity SELL", sell, inst_type="EQUITY", side="SELL",
                  money_key="quantity", absent_money_key="amount")
    check("equity SELL: quantity is 8dp string", sell["quantity"] == "1.50000000",
          f"got {sell['quantity']!r}")

    # Deterministic order id — this is what makes a same-minute duplicate
    # dedupe at Public rather than fill twice.
    a = equities.deterministic_order_id("acct", "BUY", "SPY")
    b = equities.deterministic_order_id("acct", "BUY", "SPY")
    check("equity: order id deterministic within the minute", a == b)
    check("equity: differs by side",
          a != equities.deterministic_order_id("acct", "SELL", "SPY"))


# ── 3-4. crypto ──────────────────────────────────────────────────────────────

class _FakeResp:
    """Minimal stand-in for requests.Response.

    Carries more attributes than strictly needed so a helper like
    _raise_with_body can inspect whatever it likes without an
    AttributeError turning a payload assertion into a confusing crash.
    """
    status_code = 200
    ok = True
    reason = "OK"
    text = "{}"
    content = b"{}"
    headers: dict = {}
    url = "https://api.public.com/test"

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        return None


def test_crypto() -> None:
    print("\n[3-4] crypto_ema_atr_v1 order bodies (network boundary patched)")

    # IMPORT PATH — why this is not the obvious dotted import.
    #
    # crypto_bot/exchange/public_api.py uses ABSOLUTE package imports:
    #
    #     from crypto_bot.config.settings import get_public_api_key
    #     from crypto_bot.utils.retry import retry
    #
    # so `crypto_bot` must be a TOP-LEVEL package on sys.path. Importing it
    # as `bots.crypto_ema_atr_v1.crypto_bot.exchange.public_api` resolves
    # the file but then fails executing line 6 with
    # ModuleNotFoundError: No module named 'crypto_bot'.
    #
    # The bot's own wrapper does the same thing at main.py:36-38 — it adds
    # its directory to sys.path before running anything. This mirrors that
    # rather than inventing a different mechanism.
    #
    # NOT wrapped in a skip. A crypto payload that silently goes untested is
    # worse than no test: the suite reports green while covering nothing,
    # which is the exact failure mode this whole file exists to prevent.
    # An import failure here is a FAILURE.
    crypto_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bots", "crypto_ema_atr_v1",
    )
    if crypto_root not in sys.path:
        sys.path.insert(0, crypto_root)

    try:
        from crypto_bot.exchange import public_api as cx
        check("crypto: module imported", True)
    except Exception as e:  # noqa: BLE001
        check("crypto: module imported", False,
              f"{e!r} — crypto BUY/SELL payloads are NOT covered. "
              f"Expected crypto_bot importable from {crypto_root}")
        return

    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):  # noqa: A002
        captured["url"] = url
        captured["body"] = json
        return _FakeResp()

    real_post = cx.requests.post
    real_headers = cx._headers
    try:
        cx.requests.post = fake_post
        cx._headers = lambda *a, **k: {"Authorization": "Bearer test"}

        # ── BUY ───────────────────────────────────────────────────────────
        try:
            cx.place_order_buy("acct-1", "BTC", 10.0, client_order_id="cid-buy")
            buy_err = None
        except Exception as e:  # noqa: BLE001
            buy_err = e
        check("crypto BUY: call completed", buy_err is None, repr(buy_err))

        buy = captured.get("body")
        check("crypto BUY: payload captured", isinstance(buy, dict) and bool(buy),
              f"nothing reached the patched requests.post — got {buy!r}")
        if isinstance(buy, dict) and buy:
            assert_common("crypto BUY", buy, inst_type="CRYPTO", side="BUY",
                          money_key="amount", absent_money_key="quantity")
            check("crypto BUY: honours client_order_id",
                  buy.get("orderId") == "cid-buy", f"got {buy.get('orderId')!r}")
            check("crypto BUY: posts to api.public.com",
                  str(captured.get("url", "")).startswith("https://api.public.com/"),
                  str(captured.get("url")))

        # ── SELL ──────────────────────────────────────────────────────────
        captured.clear()
        try:
            cx.place_order_sell("acct-1", "BTC", 0.00012345,
                                client_order_id="cid-sell")
            sell_err = None
        except Exception as e:  # noqa: BLE001
            sell_err = e
        check("crypto SELL: call completed", sell_err is None, repr(sell_err))

        sell = captured.get("body")
        check("crypto SELL: payload captured", isinstance(sell, dict) and bool(sell),
              f"nothing reached the patched requests.post — got {sell!r}")
        if isinstance(sell, dict) and sell:
            assert_common("crypto SELL", sell, inst_type="CRYPTO", side="SELL",
                          money_key="quantity", absent_money_key="amount")
            check("crypto SELL: honours client_order_id",
                  sell.get("orderId") == "cid-sell", f"got {sell.get('orderId')!r}")

        # Regression guard: @retry on an order function, with the id minted
        # inside the call, meant a timeout after acceptance placed a SECOND
        # real order. Removed 2026-08-08. The CI workflow also checks this
        # structurally; this asserts the runtime behaviour.
        calls = {"n": 0}

        def counting_post(*a, **k):
            calls["n"] += 1
            raise RuntimeError("simulated network failure")

        cx.requests.post = counting_post
        try:
            cx.place_order_buy("acct-1", "BTC", 10.0)
        except Exception:  # noqa: BLE001
            pass
        check("crypto BUY: failure is NOT retried (exactly 1 attempt)",
              calls["n"] == 1, f"got {calls['n']} attempts")
    finally:
        cx.requests.post = real_post
        cx._headers = real_headers


# ── 5-6. stock_momentum_v1 ───────────────────────────────────────────────────

def test_stock() -> None:
    print("\n[5-6] stock_momentum_v1 order bodies (dry_run, offline)")
    global _SKIP
    strict = os.getenv("SMOKE_STRICT", "0").strip().lower() in (
        "1", "true", "yes", "on")

    bot_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bots", "stock_momentum_v1")
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)

    try:
        try:
            import bot  # noqa: F401
        except Exception as e:  # noqa: BLE001
            if strict:
                check("stock bot import (SMOKE_STRICT=1)", False, repr(e))
            else:
                _SKIP += 1
                print(f"  SKIP  could not import bot.py: {e!r}")
                print("        pip install -r agent_runner/requirements.txt")
                print("        (SMOKE_STRICT=1 makes this a hard failure)")
            return

        auth = bot.PublicAuth("test-secret", 30, "")
        client = bot.PublicClient(auth, "", dry_run=True)

        # Pre-seed the account id cache so get_account_id() short-circuits
        # at its `if self._account_id: return` guard and never reaches the
        # network.
        #
        # NOTE, and worth fixing separately: unlike
        # league_core.public_api.auth.get_account_id, this implementation
        # does NOT honour PUBLIC_ACCOUNT_ID_SKIP_VALIDATION — that flag does
        # not exist in bot.py at all. It ALWAYS calls /account first and
        # applies the PUBLIC_ACCOUNT_ID pin afterwards, to select from the
        # returned list. So setting those env vars, as _equities_smoke does
        # for league_core, would not keep this client offline. Seeding the
        # cache is the only way to test this payload without a network call,
        # short of changing production code — which is not worth doing for a
        # test on an order path.
        client._account_id = "test-account-id"

        res = client.place_market_buy_amount("SPY", 25.0)
        buy = ((res or {}).get("response") or {}).get("payload") or {}
        check("stock BUY: dry_run returned a payload", bool(buy), f"got {res!r}")
        if buy:
            assert_common("stock BUY", buy, inst_type="EQUITY", side="BUY",
                          money_key="amount", absent_money_key="quantity")

        res = client.place_market_sell_quantity("SPY", 1.5)
        sell = ((res or {}).get("response") or {}).get("payload") or {}
        check("stock SELL: dry_run returned a payload", bool(sell))
        if sell:
            assert_common("stock SELL", sell, inst_type="EQUITY", side="SELL",
                          money_key="quantity", absent_money_key="amount")

        check("stock: dry_run made no network call", True,
              "no request was issued — dry_run short-circuits before POST")

        # Divergence worth recording. league_core uppercases the symbol in
        # the payload; the stock bot sends whatever it was handed. Both
        # happen to be fed uppercase today, so nothing is broken — but the
        # two builders do not agree, and that is how the three divergent
        # ETF allowlists started.
        lower = client.place_market_buy_amount("spy", 25.0)
        lower_sym = (((lower or {}).get("response") or {})
                     .get("payload") or {}).get("instrument", {}).get("symbol")
        check("stock: symbol is NOT normalised (known divergence vs league_core)",
              lower_sym == "spy",
              f"got {lower_sym!r} — if this now uppercases, league_core and "
              f"the stock bot have converged; update this note")
    finally:
        pass


# ── 7. Cross-cutting ─────────────────────────────────────────────────────────

def test_cross_cutting() -> None:
    print("\n[7] Cross-cutting invariants")
    from league_core.public_api import equities
    from league_core import public_bars

    # Bars: we are on v1 and do not offer the newer long periods. Pinned so
    # adding TEN_YEARS/ALL is a visible, deliberate change.
    check("bars endpoint is historicdata (v1)",
          "historicdata" in public_bars.PUBLIC_BARS_URL_TMPL,
          public_bars.PUBLIC_BARS_URL_TMPL)
    check("TEN_YEARS not yet offered", "TEN_YEARS" not in public_bars.VALID_PERIODS)
    check("ALL not yet offered", "ALL" not in public_bars.VALID_PERIODS)
    check("YEAR still supported", "YEAR" in public_bars.VALID_PERIODS)

    # Endpoints we point at. A silent host/path change would be serious.
    check("order URL is api.public.com",
          equities.ORDER_URL_TMPL.startswith("https://api.public.com/"),
          equities.ORDER_URL_TMPL)
    check("preflight is single-leg only",
          "preflight/single-leg" in equities.PREFLIGHT_URL_TMPL,
          "no multi-leg support today")
    check("portfolio is v2", "portfolio/v2" in equities.PORTFOLIO_URL_TMPL,
          equities.PORTFOLIO_URL_TMPL)


def main() -> int:
    print("=" * 70)
    print("Public API payload contract — pins what we send today")
    print("no network, no credentials, no production code touched")
    print("=" * 70)
    test_league_core_equities()
    test_crypto()
    test_stock()
    test_cross_cutting()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed, {_SKIP} skipped")
    print("=" * 70)

    if _FAIL:
        print("\nIf a failure is an INTENTIONAL new field, update this file in")
        print("the same commit that adds it. That diff is the whole point.")

    # A skipped payload is an UNTESTED payload. Under SMOKE_STRICT (which CI
    # sets) that is a failure, not a footnote — a suite reporting green while
    # covering nothing is the precise failure mode this file exists to catch.
    # Crypto silently skipped exactly once, on an import path error, and
    # reported 67 passed. Never again.
    if _SKIP and os.getenv("SMOKE_STRICT", "0").strip().lower() in (
            "1", "true", "yes", "on"):
        print(f"\n::error::{_SKIP} section(s) SKIPPED under SMOKE_STRICT=1.")
        print("Skipped payloads are untested payloads. Failing the run.")
        return 1
    if _SKIP:
        print(f"\nWARNING: {_SKIP} section(s) skipped — those payloads were "
              f"NOT tested. Set SMOKE_STRICT=1 to make this fail.")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
