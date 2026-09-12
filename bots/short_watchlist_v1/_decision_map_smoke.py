"""Smoke tests for short_watchlist_v1.decision_map — pure, dormant mapper.

Run from the repo root:

    python -m bots.short_watchlist_v1._decision_map_smoke

No network, no Supabase, no credentials, no AI. Everything under test is a
pure function over a pure dataclass.

The two tests that matter most:

  [7]  decision_map.py must import no broker, database, notifier or AI
       module. Checked by parsing the AST, not by grepping text — the
       module's own docstring discusses brokers and Supabase at length, and
       a substring check would flag the documentation as the violation it
       warns about.

  [9]  main.py must NOT import decision_map. This step is dormant by
       definition; the moment main.py imports it, that claim needs
       re-verifying and this test is what forces the conversation.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT))

from league_core import decisions as d          # noqa: E402
from league_core.decisions import DecisionValidationError  # noqa: E402
from bots.short_watchlist_v1 import screener    # noqa: E402
from bots.short_watchlist_v1 import decision_map as dm  # noqa: E402


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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def entry(symbol: str = "AMZN", **over) -> screener.EntrySignal:
    base = dict(
        symbol=symbol, close=232.11, sma50=245.0, sma200=250.0,
        rolling_low=232.50, ret_3m=-0.12, confidence=0.78,
        rationale=("close 232.11 < SMA50 245.00 & SMA200 250.00; "
                   "at/below 20-day low 232.50; 3m return -12.00%"),
    )
    base.update(over)
    return screener.EntrySignal(**base)


def ex(symbol: str = "AMZN", **over) -> screener.ExitSignal:
    base = dict(
        symbol=symbol, close=270.44, sma20=255.0, reason="stop",
        rationale="adverse move +16.51% from entry 232.11",
    )
    base.update(over)
    return screener.ExitSignal(**base)


# ── 1-2. Action mapping ──────────────────────────────────────────────────────

def test_actions() -> None:
    print("\n[1] EntrySignal -> SELL + OPEN")
    dec = dm.entry_to_decision(entry())
    check("action is SELL", dec.action == d.SELL, f"got {dec.action}")
    check("is_actionable", dec.is_actionable is True)
    # THE REGRESSION THIS SUITE CAUGHT. A short entry is a SELL that OPENS.
    # Before position_effect existed, the contract derived is_open/is_close
    # from the action alone and reported is_close=True here — backwards,
    # and exactly the case where entry-side throttles must apply.
    check("position_effect is OPEN", dec.position_effect == d.OPEN,
          f"got {dec.position_effect}")
    check("is_open (entry-side throttles apply)", dec.is_open is True)
    check("not is_close", dec.is_close is False)
    check("bot_type is 'short'", dec.bot_type == "short")
    check("intended_mode is paper", dec.intended_mode == "paper")
    check("tagged as entry", "entry" in dec.strategy_tags)

    print("\n[2] ExitSignal -> COVER + CLOSE")
    dec = dm.exit_to_decision(ex())
    check("action is COVER", dec.action == d.COVER, f"got {dec.action}")
    check("is_actionable", dec.is_actionable is True)
    check("position_effect is CLOSE", dec.position_effect == d.CLOSE)
    check("is_close (exempt from entry throttles)", dec.is_close is True)
    check("not is_open", dec.is_open is False)
    check("tagged as exit", "exit" in dec.strategy_tags)

    # The only bot exercising COVER — the reason this bot adopts first.
    check("COVER is ACTIONABLE", d.COVER in d.ACTIONABLE)

    print("\n[2b] entry and exit are opposite in effect")
    e, x = dm.entry_to_decision(entry()), dm.exit_to_decision(ex())
    check("entry opens, exit closes", e.is_open and x.is_close)
    check("no decision is both", not (e.is_close or x.is_open))


# ── 3-4. Asset classification ────────────────────────────────────────────────

def test_asset_class() -> None:
    print("\n[3] ETF symbols -> etf")
    for sym in ("SPY", "QQQ", "SCHB", "SCHD", "SGOV", "IWM", "XLK"):
        dec = dm.entry_to_decision(entry(symbol=sym))
        check(f"{sym} -> etf", dec.asset_class == "etf", f"got {dec.asset_class}")
        check(f"{sym} exit -> etf",
              dm.exit_to_decision(ex(symbol=sym)).asset_class == "etf")

    print("\n[4] Ordinary tickers -> equity")
    for sym in ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"):
        dec = dm.entry_to_decision(entry(symbol=sym))
        check(f"{sym} -> equity", dec.asset_class == "equity", f"got {dec.asset_class}")

    print("\n[4b] No fourth allowlist — delegates to league_core.common")
    from league_core import common  # noqa: E402
    # Behavioural, not textual: the mapper must agree with the canonical
    # helper on every symbol, so it cannot be carrying its own list.
    # (This bot once had a 4-symbol allowlist, options_alert_v1 had 3, and
    # stock_momentum_v1 had 30 — the same ticker got a different
    # asset_class depending on which bot logged it.)
    disagreements = [
        s for s in sorted(common.ETF_SYMBOLS | {"AAPL", "MSFT", "NVDA", "TSLA"})
        if dm.entry_to_decision(entry(symbol=s)).asset_class
        != common.classify_asset_class(s)
    ]
    check("agrees with common on every known symbol",
          not disagreements, f"disagreed on {disagreements}")
    check("module has no ETF_SYMBOLS of its own",
          not hasattr(dm, "ETF_SYMBOLS"))

    print("\n[4c] Symbol normalisation and override")
    check("lowercase normalised", dm.entry_to_decision(entry(symbol="aapl")).symbol == "AAPL")
    check("symbol arg overrides signal",
          dm.entry_to_decision(entry(symbol="AAPL"), symbol="MSFT").symbol == "MSFT")
    check("blank symbol rejected", raises(lambda: dm.entry_to_decision(entry(symbol=""))))


# ── 5. Confidence ────────────────────────────────────────────────────────────

def test_confidence() -> None:
    print("\n[5] confidence preserved and bounded")
    check("0.78 preserved", dm.entry_to_decision(entry(confidence=0.78)).confidence == 0.78)
    check("0.5 preserved", dm.entry_to_decision(entry(confidence=0.5)).confidence == 0.5)
    check("1.0 preserved", dm.entry_to_decision(entry(confidence=1.0)).confidence == 1.0)

    # screener bounds confidence to [0.5, 1.0] by construction, so these
    # should be unreachable. Clamping anyway: a bound that is documented
    # but unenforced is not a bound.
    check("1.5 clamped to 1.0", dm.entry_to_decision(entry(confidence=1.5)).confidence == 1.0)
    check("-0.2 clamped to 0.0", dm.entry_to_decision(entry(confidence=-0.2)).confidence == 0.0)
    check("NaN -> None", dm.entry_to_decision(
        entry(confidence=float("nan"))).confidence is None)
    check("None -> None", dm.entry_to_decision(entry(confidence=None)).confidence is None)
    check("garbage -> None", dm.entry_to_decision(entry(confidence="x")).confidence is None)

    print("\n[5b] exits are not scored")
    check("exit confidence is None", dm.exit_to_decision(ex()).confidence is None)

    print("\n[5c] no sizing invented by the mapper")
    for dec in (dm.entry_to_decision(entry()), dm.exit_to_decision(ex())):
        check(f"{dec.action} amount is None", dec.suggested_amount_usd is None)
        check(f"{dec.action} quantity is None", dec.suggested_quantity is None)


# ── 6. Reason handling ───────────────────────────────────────────────────────

def test_reason() -> None:
    print("\n[6] reason is required, non-empty, and preserves rationale")
    dec = dm.entry_to_decision(entry())
    check("entry reason non-empty", bool(dec.reason.strip()))
    check("entry preserves rationale", "SMA50" in dec.reason, dec.reason)

    dec = dm.exit_to_decision(ex())
    check("exit reason non-empty", bool(dec.reason.strip()))
    check("exit keeps machine-readable reason", dec.reason.startswith("stop"), dec.reason)
    check("exit keeps rationale", "adverse move" in dec.reason, dec.reason)

    # Blank rationale falls back to a synthesised, still-useful reason
    # rather than producing an unreadable row.
    dec = dm.entry_to_decision(entry(rationale=""))
    check("blank rationale -> fallback", bool(dec.reason.strip()), dec.reason)
    check("fallback names the symbol", "AMZN" in dec.reason, dec.reason)

    dec = dm.exit_to_decision(ex(rationale="", reason="trend_reversal"))
    check("exit blank rationale -> reason used", "trend_reversal" in dec.reason, dec.reason)

    # Everything blank: the symbol-based fallback still yields a readable
    # reason. That is deliberate — a decision row whose rationale is blank
    # is unreadable six weeks later, which is exactly when these get read.
    dec = dm.exit_to_decision(ex(rationale="", reason=""))
    check("wholly empty exit still gets a reason", bool(dec.reason.strip()), dec.reason)
    check("fallback names the symbol", "AMZN" in dec.reason, dec.reason)
    check("contract still rejects empty reason",
          raises(lambda: d.BotDecision(
              bot_id="x", bot_type="short", asset_class="equity",
              symbol="AMZN", action=d.COVER, reason="   "),
              DecisionValidationError))

    # _require_reason itself refuses when there is genuinely nothing.
    check("_require_reason raises on all-empty",
          raises(lambda: dm._require_reason("", "   ", None), ValueError))

    print("\n[6b] None signal rejected")
    check("entry(None) raises", raises(lambda: dm.entry_to_decision(None), ValueError))
    check("exit(None) raises", raises(lambda: dm.exit_to_decision(None), ValueError))

    print("\n[6c] stop exits are distinguishable from discretionary ones")
    stop = dm.exit_to_decision(ex(reason="stop"))
    trend = dm.exit_to_decision(ex(reason="trend_reversal"))
    check("stop tagged risk_exit", "risk_exit" in stop.strategy_tags)
    check("trend NOT tagged risk_exit", "risk_exit" not in trend.strategy_tags)
    check("stop risk_notes says risk", "Risk exit" in (stop.risk_notes or ""))
    check("trend risk_notes says not a risk limit",
          "Not a risk limit" in (trend.risk_notes or ""))


# ── 7. Import policy ─────────────────────────────────────────────────────────

def test_imports() -> None:
    print("\n[7] decision_map.py imports nothing dangerous")
    tree = ast.parse((_HERE / "decision_map.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    forbidden = {
        "requests", "httpx", "urllib", "urllib3", "http", "socket",
        "supabase", "postgrest", "psycopg2",
        "anthropic", "openai", "cohere",
        "pandas", "numpy",
    }
    check(f"no forbidden imports (found {sorted(imported)})",
          not (imported & forbidden), f"hits: {sorted(imported & forbidden)}")

    allowed = {"__future__", "typing", "league_core", "bots"}
    check("only stdlib typing + league_core + bots",
          not (imported - allowed), f"unexpected: {sorted(imported - allowed)}")

    # Stronger than a substring scan: walk the AST for any attribute access
    # or call whose name touches an order path. A docstring cannot match —
    # it is a single ast.Constant, not a Name or Attribute node.
    banned_names = {
        "public_api", "equities", "place_market_buy_amount",
        "place_market_sell_quantity", "place_order_buy", "place_order_sell",
        "trader", "discord", "notify_buy", "notify_sell",
    }
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
    check(f"no order-path identifier referenced",
          not (referenced & banned_names),
          f"hits: {sorted(referenced & banned_names)}")

    # The mapper must not even import the League logger. It is PURE: the
    # writer that persists decisions is a separate Phase C module, so a
    # mapping bug can never be a write bug.
    check("does not import league_core.status",
          "status" not in {
              n.module.split(".")[-1]
              for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module
          })


# ── 8. Purity ────────────────────────────────────────────────────────────────

def test_purity() -> None:
    print("\n[8] mapper is pure and side-effect free")
    sig = entry()
    a = dm.entry_to_decision(sig, run_id="r1")
    b = dm.entry_to_decision(sig, run_id="r1")
    check("deterministic", a.to_row() == b.to_row())

    # Input must not be mutated.
    before = (sig.symbol, sig.confidence, sig.rationale)
    dm.entry_to_decision(sig)
    check("input signal unmutated", (sig.symbol, sig.confidence, sig.rationale) == before)

    # Decisions must not share mutable state.
    a.metadata["injected"] = True
    check("instances do not share metadata", "injected" not in b.metadata)

    # Module-level constants must not drift between calls.
    check("BASE_TAGS is a tuple", isinstance(dm.BASE_TAGS, tuple))
    check("decision is frozen", raises(lambda: setattr(a, "action", d.BUY), Exception))

    tree = ast.parse((_HERE / "decision_map.py").read_text(encoding="utf-8"))
    check("no __main__ block",
          not any(isinstance(n, ast.If)
                  and getattr(getattr(n.test, "left", None), "id", None) == "__name__"
                  for n in tree.body))

    print("\n[8b] run_id and entry_price are recorded, not acted on")
    check("run_id carried", dm.entry_to_decision(entry(), run_id="abc").run_id == "abc")
    check("run_id optional", dm.entry_to_decision(entry()).run_id is None)

    e = dm.exit_to_decision(ex(), entry_price=232.11)
    check("entry_price -> metadata", e.metadata.get("entry_price") == 232.11)
    check("move_pct computed", e.metadata.get("move_pct_from_entry") is not None)
    check("move is positive (loss on a short)",
          e.metadata["move_pct_from_entry"] > 0, str(e.metadata.get("move_pct_from_entry")))
    check("sign convention documented", "LOSS" in e.metadata.get("move_sign_note", ""))
    check("bad entry_price ignored, no raise",
          dm.exit_to_decision(ex(), entry_price="abc").metadata.get("entry_price") is None)
    check("zero entry_price ignored",
          dm.exit_to_decision(ex(), entry_price=0).metadata.get("entry_price") is None)


# ── 9. Still dormant ─────────────────────────────────────────────────────────

def test_dormant() -> None:
    print("\n[9] main.py does NOT import decision_map")
    main_src = (_HERE / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "decision_map" in node.module:
                hits.append(node.module)
            for a in node.names:
                if "decision_map" in a.name:
                    hits.append(a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if "decision_map" in a.name:
                    hits.append(a.name)
    check("main.py has no decision_map import", not hits, f"found {hits}")

    # And no other running bot imports it either.
    bots_dir = _ROOT / "bots"
    offenders = []
    for py in bots_dir.rglob("*.py"):
        if py.name.startswith("_") or py.name == "decision_map.py":
            continue
        try:
            if "decision_map" in py.read_text(encoding="utf-8"):
                offenders.append(str(py.relative_to(_ROOT)))
        except Exception:  # noqa: BLE001
            continue
    check("no bot module references decision_map", not offenders, f"{offenders}")


# ── 10. Serialisation ────────────────────────────────────────────────────────

def test_serialisable() -> None:
    print("\n[10] to_row() is JSON-serialisable")
    for label, dec in (
        ("entry", dm.entry_to_decision(entry(), run_id="r1")),
        ("exit", dm.exit_to_decision(ex(), run_id="r1", entry_price=232.11)),
        ("etf entry", dm.entry_to_decision(entry(symbol="SPY"))),
    ):
        row = dec.to_row()
        try:
            encoded = json.dumps(row)
            check(f"{label} json.dumps", True)
            check(f"{label} round-trips", json.loads(encoded)["action"] == dec.action)
        except Exception as e:  # noqa: BLE001
            check(f"{label} json.dumps", False, repr(e))
        check(f"{label} tags are a list", isinstance(row["strategy_tags"], list))
        check(f"{label} no resolved_mode", "resolved_mode" not in row)
        check(f"{label} no created_at", "created_at" not in row)
        check(f"{label} metadata is a dict", isinstance(row["metadata"], dict))
        check(f"{label} position_effect present",
              row.get("position_effect") in (d.OPEN, d.CLOSE, d.NONE),
              f"got {row.get('position_effect')!r}")

    print("\n[10b] position_effect survives serialisation")
    er = dm.entry_to_decision(entry()).to_row()
    xr = dm.exit_to_decision(ex()).to_row()
    check("entry row says OPEN", er["position_effect"] == d.OPEN)
    check("exit row says CLOSE", xr["position_effect"] == d.CLOSE)
    check("both round-trip",
          json.loads(json.dumps(er))["position_effect"] == d.OPEN
          and json.loads(json.dumps(xr))["position_effect"] == d.CLOSE)


def main() -> int:
    print("=" * 68)
    print("short_watchlist_v1.decision_map — pure, dormant mapper")
    print("=" * 68)
    test_actions()
    test_asset_class()
    test_confidence()
    test_reason()
    test_imports()
    test_purity()
    test_dormant()
    test_serialisable()
    print("\n" + "=" * 68)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 68)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
