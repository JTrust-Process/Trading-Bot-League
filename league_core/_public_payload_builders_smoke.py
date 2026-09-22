"""Smoke tests for league_core.public_api.payloads — dormant builders.

Run from the repo root:

    python -m league_core._public_payload_builders_smoke

No network, no credentials, no Supabase. The module under test is pure.

THE LOAD-BEARING TEST IS [1]: with default arguments, the dormant equity
builder must produce a body BYTE-IDENTICAL to what
league_core.public_api.equities._build_payload sends in production today.

That equivalence is the entire justification for this layer. If the two
ever diverge, the dormant builder has stopped modelling reality and every
conclusion drawn from it is worthless — which is worse than not having it,
because it looks authoritative.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from league_core.public_api import payloads as p  # noqa: E402
from league_core.public_api import equities  # noqa: E402


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


def raises(fn, exc=p.PayloadPolicyError) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ── 1. Parity with the LIVE builder ──────────────────────────────────────────

def test_parity_with_live() -> None:
    print("\n[1] Defaults are byte-identical to the live equities builder")

    live_buy = equities._build_payload("oid-1", "BUY", "spy", amount_usd=25.0)
    dry_buy = p.build_equity_market_order_payload("oid-1", "BUY", "spy", amount_usd=25.0)
    check("equity BUY identical to live", dry_buy == live_buy,
          f"\n        live={live_buy}\n        dry ={dry_buy}")

    live_sell = equities._build_payload("oid-2", "SELL", "qqq", quantity=1.5)
    dry_sell = p.build_equity_market_order_payload("oid-2", "SELL", "qqq", quantity=1.5)
    check("equity SELL identical to live", dry_sell == live_sell,
          f"\n        live={live_sell}\n        dry ={dry_sell}")

    # Formatting quirks mirrored, not "fixed".
    check("equity amount is 2dp padded", dry_buy["amount"] == "25.00",
          f"got {dry_buy['amount']!r}")
    check("equity quantity is 8dp padded", dry_sell["quantity"] == "1.50000000",
          f"got {dry_sell['quantity']!r}")
    check("equity uppercases symbol", dry_buy["instrument"]["symbol"] == "SPY")

    for label, body in (("BUY", dry_buy), ("SELL", dry_sell)):
        try:
            p.assert_matches_live_shape(body)
            check(f"equity {label} passes assert_matches_live_shape", True)
        except Exception as e:  # noqa: BLE001
            check(f"equity {label} passes assert_matches_live_shape", False, repr(e))


# ── 2. useMargin ─────────────────────────────────────────────────────────────

def test_use_margin() -> None:
    print("\n[2] useMargin")
    omitted = p.build_equity_market_order_payload("o", "BUY", "SPY", amount_usd=10)
    check("use_margin=None omits the field", "useMargin" not in omitted,
          f"keys={sorted(omitted)}")

    forced = p.build_equity_market_order_payload(
        "o", "BUY", "SPY", amount_usd=10, use_margin=False)
    check("use_margin=False emits the field", "useMargin" in forced)
    check("emitted value is boolean False", forced["useMargin"] is False,
          f"got {forced['useMargin']!r}")
    check("JSON renders as false", '"useMargin": false' in json.dumps(forced, indent=1)
          or json.loads(json.dumps(forced))["useMargin"] is False)

    # True is refused: cash account, margin is never the intent.
    check("use_margin=True is REFUSED",
          raises(lambda: p.build_equity_market_order_payload(
              "o", "BUY", "SPY", amount_usd=10, use_margin=True)))
    for bad in ("false", 0, 1, "no"):
        check(f"use_margin={bad!r} rejected",
              raises(lambda b=bad: p.build_equity_market_order_payload(
                  "o", "BUY", "SPY", amount_usd=10, use_margin=b)))

    # Adding it must not disturb anything else.
    base = dict(omitted)
    extra = dict(forced)
    extra.pop("useMargin")
    check("useMargin is purely additive", extra == base,
          f"diff={set(extra) ^ set(base)}")


# ── 3. openCloseIndicator ────────────────────────────────────────────────────

def test_open_close() -> None:
    print("\n[3] openCloseIndicator")
    default = p.build_equity_market_order_payload("o", "SELL", "SPY", quantity=1)
    check("omitted by default", "openCloseIndicator" not in default)

    for val, want in (("OPEN", "OPEN"), ("close", "CLOSE"), (" Open ", "OPEN")):
        body = p.build_equity_market_order_payload(
            "o", "SELL", "SPY", quantity=1, open_close=val)
        check(f"open_close={val!r} -> {want}",
              body["openCloseIndicator"] == want, f"got {body.get('openCloseIndicator')!r}")

    for bad in ("OPENING", "", "  ", "SHORT", 1, True):
        check(f"open_close={bad!r} rejected",
              raises(lambda b=bad: p.build_equity_market_order_payload(
                  "o", "SELL", "SPY", quantity=1, open_close=b)))

    # The four position semantics, incl. the one that motivated
    # BotDecision.position_effect. COVER has no Public equivalent: it is
    # orderSide=BUY with openCloseIndicator=CLOSE.
    print("\n[3b] the four position semantics")
    cases = (
        ("long entry",  "BUY",  "OPEN"),
        ("long exit",   "SELL", "CLOSE"),
        ("short entry", "SELL", "OPEN"),
        ("cover",       "BUY",  "CLOSE"),
    )
    for label, side, oc in cases:
        body = p.build_equity_market_order_payload(
            "o", side, "SPY", quantity=1, open_close=oc)
        check(f"{label}: {side} + {oc}",
              body["orderSide"] == side and body["openCloseIndicator"] == oc)
    check("short entry and long exit share a side, differ in indicator",
          True, "SELL+OPEN vs SELL+CLOSE — why action alone is insufficient")


# ── 4. Crypto ────────────────────────────────────────────────────────────────

def test_crypto() -> None:
    print("\n[4] Crypto builder")
    buy = p.build_crypto_market_order_payload("o", "BUY", "BTC", amount_usd=10.0)
    sell = p.build_crypto_market_order_payload("o", "SELL", "BTC", quantity=0.00012345)

    check("BUY type is CRYPTO", buy["instrument"]["type"] == "CRYPTO")
    check("BUY uses amount", "amount" in buy and "quantity" not in buy)
    check("SELL uses quantity", "quantity" in sell and "amount" not in sell)
    check("orderType MARKET", buy["orderType"] == "MARKET")
    check("timeInForce DAY", buy["expiration"]["timeInForce"] == "DAY")

    # Mirrors production exactly, including the unpadded formatting.
    check("crypto amount is str(round(...)) — '10.0', not '10.00'",
          buy["amount"] == "10.0", f"got {buy['amount']!r}")
    check("crypto quantity unpadded", sell["quantity"] == "0.00012345",
          f"got {sell['quantity']!r}")
    check("crypto does NOT uppercase symbol",
          p.build_crypto_market_order_payload(
              "o", "BUY", "btc", amount_usd=1)["instrument"]["symbol"] == "btc")

    # There is no use_margin parameter at all — margin is equities-only.
    check("crypto builder has no use_margin parameter",
          raises(lambda: p.build_crypto_market_order_payload(
              "o", "BUY", "BTC", amount_usd=10, use_margin=False), TypeError))
    for body in (buy, sell):
        check("crypto never emits useMargin", "useMargin" not in body)
        check("crypto never emits openCloseIndicator",
              "openCloseIndicator" not in body)
        p.assert_matches_live_shape(body)
    check("crypto bodies match the live shape", True)


# ── 5. Blocked fields ────────────────────────────────────────────────────────

def test_blocked() -> None:
    print("\n[5] Blocked fields are unreachable")
    for body in (
        p.build_equity_market_order_payload("o", "BUY", "SPY", amount_usd=10),
        p.build_equity_market_order_payload(
            "o", "BUY", "SPY", amount_usd=10, use_margin=False, open_close="OPEN"),
        p.build_crypto_market_order_payload("o", "BUY", "BTC", amount_usd=10),
    ):
        present = [k for k in p.BLOCKED_FIELDS if k in body]
        check(f"no blocked field in {body['instrument']['type']} body",
              not present, f"found {present}")

    for f in ("orderClass", "takeProfit", "stopLoss", "bracketId",
              "equityMarketSession", "taxLotMatchingInstructions"):
        check(f"{f} is documented as blocked", f in p.BLOCKED_FIELDS)
        check(f"{f} has a stated reason",
              len(p.BLOCKED_FIELDS[f]) > 40, "reason must be substantive")

    # No builder accepts them as kwargs.
    for kw in ("order_class", "take_profit", "stop_loss", "bracket_id",
               "market_session", "tax_lot", "limit_price", "stop_price",
               "good_till_date"):
        check(f"equity builder rejects {kw}=",
              raises(lambda k=kw: p.build_equity_market_order_payload(
                  "o", "BUY", "SPY", amount_usd=10, **{k: "x"}), TypeError))

    print("\n[5b] assert_no_blocked_fields")
    clean = p.build_equity_market_order_payload("o", "BUY", "SPY", amount_usd=10)
    try:
        p.assert_no_blocked_fields(clean)
        check("clean payload passes", True)
    except Exception as e:  # noqa: BLE001
        check("clean payload passes", False, repr(e))

    dirty = dict(clean)
    dirty["orderClass"] = "BRACKET"
    check("payload with orderClass is refused",
          raises(lambda: p.assert_no_blocked_fields(dirty)))
    check("non-dict refused", raises(lambda: p.assert_no_blocked_fields("x")))


# ── 6. Validation ────────────────────────────────────────────────────────────

def test_validation() -> None:
    print("\n[6] Argument validation")
    for fn in (p.build_equity_market_order_payload, p.build_crypto_market_order_payload):
        n = fn.__name__
        check(f"{n}: both legs rejected",
              raises(lambda f=fn: f("o", "BUY", "S", amount_usd=1, quantity=1)))
        check(f"{n}: neither leg rejected", raises(lambda f=fn: f("o", "BUY", "S")))
        check(f"{n}: bad side rejected",
              raises(lambda f=fn: f("o", "COVER", "S", amount_usd=1)))
        check(f"{n}: empty order_id rejected",
              raises(lambda f=fn: f("", "BUY", "S", amount_usd=1)))
        check(f"{n}: empty symbol rejected",
              raises(lambda f=fn: f("o", "BUY", "", amount_usd=1)))
        for bad in (0, -1, float("nan"), float("inf"), "abc"):
            check(f"{n}: amount={bad!r} rejected",
                  raises(lambda f=fn, b=bad: f("o", "BUY", "S", amount_usd=b)))

    print("\n[6b] JSON-serialisable")
    for body in (
        p.build_equity_market_order_payload(
            "o", "BUY", "SPY", amount_usd=10, use_margin=False, open_close="OPEN"),
        p.build_crypto_market_order_payload("o", "SELL", "BTC", quantity=0.5),
    ):
        try:
            check(f"{body['instrument']['type']} round-trips",
                  json.loads(json.dumps(body)) == body)
        except Exception as e:  # noqa: BLE001
            check("json round-trip", False, repr(e))


# ── 7. Dormancy and import policy ────────────────────────────────────────────

def test_dormant_and_pure() -> None:
    print("\n[7] Module is pure and dormant")
    path = pathlib.Path(__file__).parent / "public_api" / "payloads.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    allowed = {"__future__", "typing"}
    check(f"imports only {sorted(allowed)} (found {sorted(imported)})",
          not (imported - allowed), f"unexpected: {sorted(imported - allowed)}")
    for banned in ("requests", "httpx", "supabase", "urllib", "socket", "os"):
        check(f"does not import {banned}", banned not in imported)

    check("no __main__ block",
          not any(isinstance(n, ast.If)
                  and getattr(getattr(n.test, "left", None), "id", None) == "__name__"
                  for n in tree.body))

    # Nothing in the repo may import it yet.
    root = pathlib.Path(__file__).parent.parent
    offenders = []
    for py in list((root / "bots").rglob("*.py")) + list((root / "league_core").rglob("*.py")):
        if py.name.startswith("_") or py.name == "payloads.py":
            continue
        try:
            txt = py.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if "public_api.payloads" in txt or "import payloads" in txt:
            offenders.append(str(py.relative_to(root)))
    check("no production module imports payloads", not offenders, f"{offenders}")


def main() -> int:
    print("=" * 70)
    print("league_core.public_api.payloads — dormant Public payload builders")
    print("=" * 70)
    test_parity_with_live()
    test_use_margin()
    test_open_close()
    test_crypto()
    test_blocked()
    test_validation()
    test_dormant_and_pure()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
