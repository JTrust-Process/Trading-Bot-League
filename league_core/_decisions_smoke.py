"""Smoke tests for league_core.decisions — the dormant BotDecision contract.

Run from the repo root:

    python -m league_core._decisions_smoke

No network, no Supabase, no credentials, no AI. The module under test is
pure, so these are ordinary unit assertions.

Two of these tests matter more than the rest:

  [11]/[12]  A bot must not be able to supply `resolved_mode` or
             `created_at`. Those fields are absent from the dataclass, so
             passing them raises TypeError. This is a STRUCTURAL guarantee
             rather than a validation rule — there is no code path that
             could be edited to "allow it just this once".

  [14]       decisions.py must not import a broker, Supabase, requests,
             a notifier, or an AI SDK. Checked by parsing the AST, not by
             grepping text: the module's own docstring discusses brokers
             and Supabase at length, and a substring check would flag the
             documentation as the violation it warns about.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from league_core import decisions as d  # noqa: E402


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


def raises(fn, exc=d.DecisionValidationError) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def valid(**overrides):
    """A minimal valid decision, with overrides applied."""
    base = dict(
        bot_id="stock_momentum_v1",
        bot_type="stock",
        asset_class="equity",
        symbol="AAPL",
        action=d.BUY,
        reason="momentum rank=1",
    )
    base.update(overrides)
    return d.BotDecision(**base)


# ── 1-2. Action groups ───────────────────────────────────────────────────────

def test_action_groups() -> None:
    print("\n[1] BUY / SELL / COVER are actionable")
    for a in (d.BUY, d.SELL, d.COVER):
        dec = valid(action=a)
        check(f"{a} is_actionable", dec.is_actionable is True)
        check(f"{a} in ACTIONABLE", a in d.ACTIONABLE)

    print("\n[2] HOLD / SKIP / ALERT are NOT actionable")
    for a in (d.HOLD, d.SKIP, d.ALERT):
        dec = valid(action=a)
        check(f"{a} is_actionable is False", dec.is_actionable is False)
        check(f"{a} in NON_ACTIONABLE", a in d.NON_ACTIONABLE)

    check("groups are disjoint", not (d.ACTIONABLE & d.NON_ACTIONABLE))
    check("groups cover ACTIONS", (d.ACTIONABLE | d.NON_ACTIONABLE) == d.ACTIONS)

    # Exits must be distinguishable so an executor can exempt them from
    # entry-side throttles. Five separate incidents in this system came
    # from failing to make that distinction.
    check("SELL is_close", valid(action=d.SELL).is_close is True)
    check("COVER is_close", valid(action=d.COVER).is_close is True)
    check("BUY is_open", valid(action=d.BUY).is_open is True)
    check("BUY not is_close", valid(action=d.BUY).is_close is False)
    check("SKIP neither open nor close",
          not valid(action=d.SKIP).is_close and not valid(action=d.SKIP).is_open)

    # There must be no way to mark a non-actionable decision executable.
    dec = valid(action=d.SKIP)
    check("is_actionable has no setter",
          raises(lambda: setattr(dec, "is_actionable", True), Exception))
    check("decision is frozen",
          raises(lambda: setattr(dec, "action", d.BUY), Exception))


# ── 3-4. Vocabulary ──────────────────────────────────────────────────────────

def test_invalid_vocabulary() -> None:
    print("\n[3] Invalid action rejected")
    for bad in ("PURCHASE", "buy_now", "", "   ", "EXECUTE", None, 7):
        check(f"action={bad!r} rejected", raises(lambda b=bad: valid(action=b)))
    check("lowercase 'buy' normalises to BUY", valid(action="buy").action == d.BUY)
    check("whitespace tolerated", valid(action="  sell  ").action == d.SELL)

    print("\n[4] Invalid asset_class rejected")
    for bad in ("stock", "fx", "", "cash", "fund", None, 3):
        check(f"asset_class={bad!r} rejected",
              raises(lambda b=bad: valid(asset_class=b)))
    for good in sorted(d.ASSET_CLASSES):
        check(f"asset_class={good!r} accepted", valid(asset_class=good).asset_class == good)
    check("uppercase ETF normalises", valid(asset_class="ETF").asset_class == "etf")

    print("\n[4b] Invalid bot_type rejected")
    check("bot_type='banana' rejected", raises(lambda: valid(bot_type="banana")))
    check("bot_type='agent_research' accepted",
          valid(bot_type="agent_research").bot_type == "agent_research")

    print("\n[4c] Required fields")
    for name in ("bot_id", "bot_type", "asset_class", "symbol", "action", "reason"):
        check(f"empty {name} rejected", raises(lambda n=name: valid(**{n: ""})))
        check(f"whitespace {name} rejected", raises(lambda n=name: valid(**{n: "   "})))
        check(f"None {name} rejected", raises(lambda n=name: valid(**{n: None})))


# ── 5-7. Numerics ────────────────────────────────────────────────────────────

def test_numerics() -> None:
    print("\n[5] confidence must be within [0, 1]")
    for good in (0.0, 0.5, 1.0, 0, 1, "0.75"):
        check(f"confidence={good!r} accepted", valid(confidence=good).confidence is not None)
    for bad in (-0.01, 1.01, 2, -1, 100, float("nan"), float("inf"), "abc"):
        check(f"confidence={bad!r} rejected", raises(lambda b=bad: valid(confidence=b)))
    check("confidence=None allowed", valid(confidence=None).confidence is None)
    check("confidence coerced to float", isinstance(valid(confidence="0.5").confidence, float))

    print("\n[6] suggested_amount_usd must not be negative")
    check("0.0 allowed", valid(suggested_amount_usd=0.0).suggested_amount_usd == 0.0)
    check("15.25 allowed", valid(suggested_amount_usd=15.25).suggested_amount_usd == 15.25)
    for bad in (-0.01, -100, float("nan"), float("inf"), "abc"):
        check(f"amount={bad!r} rejected", raises(lambda b=bad: valid(suggested_amount_usd=b)))
    check("None allowed", valid(suggested_amount_usd=None).suggested_amount_usd is None)

    print("\n[7] suggested_quantity must not be negative")
    check("0.0 allowed", valid(suggested_quantity=0.0).suggested_quantity == 0.0)
    check("0.00427 allowed", valid(suggested_quantity=0.00427).suggested_quantity == 0.00427)
    for bad in (-0.00427380, -1, float("nan"), float("inf"), "abc"):
        check(f"quantity={bad!r} rejected", raises(lambda b=bad: valid(suggested_quantity=b)))
    check("None allowed", valid(suggested_quantity=None).suggested_quantity is None)


# ── 8-10. Normalisation and defaults ─────────────────────────────────────────

def test_normalisation_and_defaults() -> None:
    print("\n[8] symbol normalised to uppercase")
    check("'aapl' -> 'AAPL'", valid(symbol="aapl").symbol == "AAPL")
    check("'  msft ' -> 'MSFT'", valid(symbol="  msft ").symbol == "MSFT")
    check("'BTC-USD' preserved", valid(symbol="btc-usd").symbol == "BTC-USD")

    print("\n[9] metadata defaults to an empty dict")
    dec = valid()
    check("defaults to {}", dec.metadata == {})
    check("is a dict", isinstance(dec.metadata, dict))
    check("None -> {}", valid(metadata=None).metadata == {})
    check("non-dict rejected", raises(lambda: valid(metadata=["a"])))

    # Two decisions must not share one dict, and a caller mutating its own
    # dict afterwards must not mutate a recorded decision.
    a, b = valid(), valid()
    a.metadata["x"] = 1
    check("instances do not share metadata", b.metadata == {}, f"b={b.metadata}")
    src = {"k": "v"}
    c = valid(metadata=src)
    src["k"] = "MUTATED"
    check("metadata copied defensively", c.metadata["k"] == "v", f"got {c.metadata}")

    print("\n[10] strategy_tags defaults to empty")
    check("defaults to ()", valid().strategy_tags == ())
    check("None -> ()", valid(strategy_tags=None).strategy_tags == ())
    check("list accepted", valid(strategy_tags=["momentum", "breakout"]).strategy_tags
          == ("momentum", "breakout"))
    check("blanks dropped", valid(strategy_tags=["a", "", "  ", "b"]).strategy_tags == ("a", "b"))
    check("bare string rejected", raises(lambda: valid(strategy_tags="momentum")))
    tags = ["x"]
    e = valid(strategy_tags=tags)
    tags.append("y")
    check("tags copied defensively", e.strategy_tags == ("x",), f"got {e.strategy_tags}")


# ── 11-12. The trust boundary ────────────────────────────────────────────────

def test_trust_boundary() -> None:
    print("\n[11] bot-supplied resolved_mode is NOT accepted")
    check("resolved_mode kwarg raises TypeError",
          raises(lambda: valid(resolved_mode="live"), TypeError))
    check("no resolved_mode attribute", not hasattr(valid(), "resolved_mode"))
    check("not in to_row()", "resolved_mode" not in valid().to_row())

    print("\n[12] bot-supplied created_at is NOT accepted")
    check("created_at kwarg raises TypeError",
          raises(lambda: valid(created_at="2020-01-01T00:00:00Z"), TypeError))
    check("no created_at attribute", not hasattr(valid(), "created_at"))
    check("not in to_row()", "created_at" not in valid().to_row())

    print("\n[12b] no authority flag is accepted")
    for f in ("can_place_orders", "manual_approval_required", "approved",
              "is_live", "force", "bypass_risk"):
        check(f"{f} kwarg raises TypeError",
              raises(lambda k=f: valid(**{k: True}), TypeError))

    print("\n[12c] intended_mode is advisory and validated")
    check("'paper' accepted", valid(intended_mode="paper").intended_mode == "paper")
    check("'LIVE' normalised", valid(intended_mode="LIVE").intended_mode == "live")
    check("None allowed", valid().intended_mode is None)
    check("'godmode' rejected", raises(lambda: valid(intended_mode="godmode")))


# ── 13. Serialisation ────────────────────────────────────────────────────────

def test_to_row() -> None:
    print("\n[13] to_row() is JSON-serialisable")
    dec = valid(
        action=d.SKIP, symbol="nvda", confidence=0.42,
        suggested_amount_usd=12.5, suggested_quantity=0.1,
        strategy_tags=["momentum"], risk_notes="below min order",
        metadata={"rank": 4, "nested": {"a": [1, 2]}},
        run_id="abc-123", intended_mode="paper",
    )
    row = dec.to_row()
    check("returns a dict", isinstance(row, dict))
    try:
        encoded = json.dumps(row)
        check("json.dumps succeeds", True)
        check("round-trips", json.loads(encoded)["symbol"] == "NVDA")
    except Exception as e:  # noqa: BLE001
        check("json.dumps succeeds", False, repr(e))

    check("symbol normalised in row", row["symbol"] == "NVDA")
    check("tags are a list, not a tuple", isinstance(row["strategy_tags"], list))
    check("is_actionable present and False", row["is_actionable"] is False)
    check("action preserved", row["action"] == "SKIP")
    check("metadata nested preserved", row["metadata"]["nested"]["a"] == [1, 2])

    row["metadata"]["injected"] = True
    check("to_row() metadata is a copy", "injected" not in dec.metadata)

    expected = {
        "bot_id", "bot_type", "asset_class", "symbol", "action", "reason",
        "confidence", "suggested_amount_usd", "suggested_quantity",
        "strategy_tags", "risk_notes", "metadata", "run_id",
        "intended_mode", "is_actionable",
    }
    check("exact key set", set(row) == expected,
          f"diff={set(row) ^ expected}")


# ── 14. Import policy ────────────────────────────────────────────────────────

def test_no_forbidden_imports() -> None:
    print("\n[14] decisions.py imports nothing dangerous")
    path = pathlib.Path(__file__).parent / "decisions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

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
        "supabase", "postgrest", "psycopg2", "sqlalchemy",
        "anthropic", "openai", "google", "cohere",
        "pandas", "numpy",
    }
    hits = imported & forbidden
    check(f"no forbidden top-level imports (found {sorted(imported)})",
          not hits, f"forbidden: {sorted(hits)}")

    allowed = {"__future__", "math", "dataclasses", "typing", "league_core"}
    unexpected = imported - allowed
    check("only stdlib + league_core", not unexpected, f"unexpected: {sorted(unexpected)}")

    # A deferred import inside a function would evade the checks above.
    nested = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and any(isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                for p in ast.walk(tree)
                if hasattr(p, "body") and n in getattr(p, "body", []))
    ]
    check("no function-level imports", not nested, f"found {len(nested)}")

    # The module must be importable with no side effects and no env.
    check("module has no __main__ side effects",
          not any(isinstance(n, ast.If) and getattr(
              getattr(n.test, "left", None), "id", None) == "__name__"
              for n in tree.body))


def main() -> int:
    print("=" * 66)
    print("league_core.decisions — dormant BotDecision contract")
    print("=" * 66)
    test_action_groups()
    test_invalid_vocabulary()
    test_numerics()
    test_normalisation_and_defaults()
    test_trust_boundary()
    test_to_row()
    test_no_forbidden_imports()
    print("\n" + "=" * 66)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 66)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
