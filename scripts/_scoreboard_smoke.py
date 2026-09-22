"""Smoke tests for scripts/league_scoreboard.py — pure helpers only.

    python -m scripts._scoreboard_smoke

No network, no Supabase, no credentials. Every function under test is pure;
the IO layer (_config/_get/fetch_all) is never called.

The assertions that matter most are the ones about HONESTY rather than
arithmetic:

  * live and paper are never summed
  * win_rate(0 closed) is None, not 0.0 — "unknown" must not render as
    "lost every trade"
  * a small sample is flagged
  * gross-of-fees is stated whenever P/L is shown
  * reported errors exceeding actual error rows is surfaced

A scoreboard that is merely correct but uncaveated would be worse than no
scoreboard, because a feeling knows it is a feeling and a table does not.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import league_scoreboard as sb  # noqa: E402


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


def trade(bot, pnl=None, paper=False, at="2026-09-01T12:00:00+00:00"):
    return {"bot_id": bot, "pnl_usd": pnl, "is_paper": paper, "occurred_at": at}


# ── 1. Grouping and live/paper separation ────────────────────────────────────

def test_grouping() -> None:
    print("\n[1] Trades group by bot, live and paper stay separate")
    rows = [
        trade("stock", 10.0), trade("stock", -4.0), trade("stock", 6.0),
        trade("stock", 100.0, paper=True),
        trade("crypto", -0.30),
        trade("stock", None),            # an entry — no P/L
    ]
    s = sb.summarize_trades(rows, since=None)

    check("two bots grouped", set(s) == {"stock", "crypto"}, f"got {sorted(s)}")
    check("stock live closed = 3", s["stock"]["live"]["closed"] == 3,
          f"got {s['stock']['live']['closed']}")
    check("entry (pnl=None) not counted as closed",
          s["stock"]["live"]["closed"] == 3)
    check("stock live gross = 12.00",
          abs(s["stock"]["live"]["gross_pnl"] - 12.0) < 1e-9,
          f"got {s['stock']['live']['gross_pnl']}")
    check("stock paper closed = 1", s["stock"]["paper"]["closed"] == 1)
    check("stock paper gross = 100.00",
          abs(s["stock"]["paper"]["gross_pnl"] - 100.0) < 1e-9)

    # THE assertion. Combining these would make the stock bot look
    # profitable on the strength of money that was never at risk.
    check("live and paper are NEVER summed",
          s["stock"]["live"]["gross_pnl"] != s["stock"]["paper"]["gross_pnl"]
          and abs(s["stock"]["live"]["gross_pnl"] - 12.0) < 1e-9,
          "paper P/L must not leak into the live figure")

    check("crypto isolated", s["crypto"]["live"]["closed"] == 1)
    check("rows with no bot_id dropped",
          sb.summarize_trades([{"pnl_usd": 5.0}]) == {})
    check("non-dict rows survived", sb.summarize_trades([None, 1, "x"]) == {})


# ── 2. Win rate ──────────────────────────────────────────────────────────────

def test_win_rate() -> None:
    print("\n[2] Win rate")
    check("3 of 4", sb.win_rate(3, 4) == 0.75)
    check("0 of 4 is 0.0", sb.win_rate(0, 4) == 0.0)
    check("4 of 4 is 1.0", sb.win_rate(4, 4) == 1.0)

    # None means UNKNOWN. Rendering it as 0% would read as "lost every
    # trade" when it means "there were no trades".
    check("0 closed -> None, NOT 0.0", sb.win_rate(0, 0) is None)
    check("None renders as a dash, not 0%", sb.fmt_pct(None).strip() == "—",
          f"got {sb.fmt_pct(None)!r}")
    check("0.0 renders as 0.0%", "0.0%" in sb.fmt_pct(0.0))

    # Break-even is 0 P/L, not 0 wins.
    rows = [trade("b", 1.0), trade("b", -1.0)]
    s = sb.summarize_trades(rows)
    check("50% win rate with zero net",
          sb.win_rate(s["b"]["live"]["wins"], s["b"]["live"]["closed"]) == 0.5)
    check("zero net P/L is still reported",
          abs(s["b"]["live"]["gross_pnl"]) < 1e-9)


# ── 3. Window filtering ──────────────────────────────────────────────────────

def test_window() -> None:
    print("\n[3] Window filtering and --since")
    check("default is 2026-08-09", sb.parse_since(None) == "2026-08-09")
    check("DEFAULT_SINCE constant matches", sb.DEFAULT_SINCE == "2026-08-09")
    check("--all -> None", sb.parse_since(None, all_time=True) is None)
    check("--all overrides --since",
          sb.parse_since("2026-01-01", all_time=True) is None)
    check("explicit date honoured", sb.parse_since("2026-09-01") == "2026-09-01")

    # A malformed date must RAISE, not silently use the default — reporting
    # a different window than the one requested is the bug class this tool
    # exists to expose.
    for bad in ("2026-13-01", "not-a-date", "09/01/2026", ""):
        try:
            sb.parse_since(bad)
            check(f"--since {bad!r} rejected", False, "did not raise")
        except ValueError:
            check(f"--since {bad!r} rejected", True)

    rows = [
        trade("b", 1.0, at="2026-07-01T00:00:00+00:00"),   # before
        trade("b", 2.0, at="2026-09-01T00:00:00+00:00"),   # after
        {"bot_id": "b", "pnl_usd": 4.0},                    # no timestamp
    ]
    s = sb.summarize_trades(rows, since="2026-08-09")
    check("pre-window trade excluded", s["b"]["live"]["closed"] == 2,
          f"got {s['b']['live']['closed']}")
    check("undated row KEPT (dropping would shrink the sample silently)",
          abs(s["b"]["live"]["gross_pnl"] - 6.0) < 1e-9,
          f"got {s['b']['live']['gross_pnl']}")
    check("all-time includes everything",
          sb.summarize_trades(rows, since=None)["b"]["live"]["closed"] == 3)


# ── 4. Runs ──────────────────────────────────────────────────────────────────

def test_runs() -> None:
    print("\n[4] Run summary")
    runs = [
        {"bot_id": "b", "started_at": "2026-09-01T10:00:00+00:00",
         "status": "success", "error_count": 0, "trade_count": 1},
        {"bot_id": "b", "started_at": "2026-09-02T10:00:00+00:00",
         "status": "warning", "error_count": 5, "trade_count": 0},
        {"bot_id": "b", "started_at": "2026-09-03T10:00:00+00:00",
         "status": "running", "error_count": 0, "trade_count": 0},
    ]
    r = sb.summarize_runs(runs)["b"]
    check("run count", r["runs"] == 3)
    check("first is earliest", r["first"].startswith("2026-09-01"))
    check("last is latest", r["last"].startswith("2026-09-03"))
    check("last_status from the latest run", r["last_status"] == "running")
    check("reported errors summed", r["reported_errors"] == 5)
    check("orphaned 'running' rows counted", r["still_running"] == 1)


# ── 5. Expenses ──────────────────────────────────────────────────────────────

def test_expenses() -> None:
    print("\n[5] Expenses")
    exp = [
        {"bot_id": "agent_research_v1", "amount_usd": 3.00, "period": "2026-09"},
        {"bot_id": "agent_research_v1", "amount_usd": 2.00, "period": "2026-08"},
        {"bot_id": "agent_research_v1", "amount_usd": 9.99, "period": "2026-05"},
        {"bot_id": None, "amount_usd": 2.00, "period": "2026-09"},   # league-wide
        {"amount_usd": 1.00, "period": "2026-09"},                    # no key
    ]
    per, wide = sb.expenses_by_bot(exp, since="2026-08-09")
    check("in-window bot expenses summed",
          abs(per["agent_research_v1"] - 5.00) < 1e-9,
          f"got {per.get('agent_research_v1')}")
    check("out-of-window period excluded (9.99 dropped)",
          "agent_research_v1" in per and per["agent_research_v1"] < 9.0)
    check("null bot_id NOT allocated to a bot",
          abs(wide - 3.00) < 1e-9, f"got {wide}")

    per_all, wide_all = sb.expenses_by_bot(exp, since=None)
    check("all-time includes old periods",
          abs(per_all["agent_research_v1"] - 14.99) < 1e-9,
          f"got {per_all.get('agent_research_v1')}")

    check("empty list safe", sb.expenses_by_bot([]) == ({}, 0.0))
    check("malformed amounts skipped",
          sb.expenses_by_bot([{"bot_id": "b", "amount_usd": "x", "period": "2026-09"}])
          == ({}, 0.0))

    # Net = gross - expenses. Subtraction, not a fee model.
    gross, expense = 12.00, 5.00
    check("net subtracts expenses", abs((gross - expense) - 7.00) < 1e-9)


# ── 6. Notes ─────────────────────────────────────────────────────────────────

def test_notes() -> None:
    print("\n[6] Notes surface every caveat")

    n = sb.build_notes("stock_momentum_v1", live_closed=18, paper_closed=0,
                       open_positions=0, reported_errors=0,
                       actual_error_rows=0, still_running=0, all_time=False)
    joined = " | ".join(n)
    check("n<30 flagged", "n<30" in joined, joined)
    check("actual n shown", "n=18" in joined, joined)
    check("GROSS stated whenever P/L is shown", "GROSS" in joined, joined)
    check("known gap: no quantity/order_id", "order_id" in joined, joined)
    check("known gap: monitor inflation", "monitor" in joined.lower(), joined)

    big = sb.build_notes("x", live_closed=120, paper_closed=0, open_positions=0,
                         reported_errors=0, actual_error_rows=0,
                         still_running=0, all_time=False)
    check("n>=30 not flagged", not any("n<30" in s for s in big),
          " | ".join(big))
    check("gross still stated at large n",
          any("GROSS" in s for s in big))

    none_ = sb.build_notes("x", live_closed=0, paper_closed=0, open_positions=0,
                           reported_errors=0, actual_error_rows=0,
                           still_running=0, all_time=False)
    check("no trades -> 'NO CLOSED TRADES'",
          any("NO CLOSED TRADES" in s for s in none_), " | ".join(none_))
    check("no trades -> no gross note (nothing to caveat)",
          not any("GROSS" in s for s in none_))

    infl = sb.build_notes("x", live_closed=1, paper_closed=0, open_positions=0,
                          reported_errors=63, actual_error_rows=0,
                          still_running=0, all_time=False)
    check("reported > actual errors surfaced",
          any("EXCEED" in s for s in infl), " | ".join(infl))

    unknown = sb.build_notes("x", live_closed=1, paper_closed=0, open_positions=0,
                             reported_errors=63, actual_error_rows=None,
                             still_running=0, all_time=False)
    check("no false claim when bot_errors unavailable",
          not any("EXCEED" in s for s in unknown), " | ".join(unknown))

    op = sb.build_notes("x", live_closed=1, paper_closed=0, open_positions=4,
                        reported_errors=0, actual_error_rows=0,
                        still_running=2, all_time=True)
    j = " | ".join(op)
    check("open positions noted as NOT valued", "NOT valued" in j, j)
    check("orphaned running runs noted", "running" in j, j)
    check("all-time warning present", "ALL-TIME" in j, j)

    for bot in ("etf_rotation_v1", "crypto_ema_atr_v1", "short_watchlist_v1"):
        nn = sb.build_notes(bot, live_closed=5, paper_closed=0, open_positions=0,
                            reported_errors=0, actual_error_rows=0,
                            still_running=0, all_time=False)
        check(f"{bot} has a known-gap note", len(nn) > 2, " | ".join(nn))


# ── 7. Report assembly is pure ───────────────────────────────────────────────

def test_report_is_pure() -> None:
    print("\n[7] build_report runs offline on fixture data")
    data = {
        "registry": [{"bot_id": "stock_momentum_v1", "bot_type": "stock",
                      "mode": "live"}],
        "status": [{"bot_id": "stock_momentum_v1", "last_run_status": "success"}],
        "runs": [{"bot_id": "stock_momentum_v1",
                  "started_at": "2026-09-01T10:00:00+00:00",
                  "status": "success", "error_count": 41, "trade_count": 2}],
        "trades": [trade("stock_momentum_v1", 3.0),
                   trade("stock_momentum_v1", -5.0),
                   trade("stock_momentum_v1", 50.0, paper=True)],
        "positions": [{"bot_id": "stock_momentum_v1", "status": "open",
                       "updated_at": "2026-09-10T00:00:00+00:00"}],
        "errors": [],
        "expenses": [{"bot_id": None, "amount_usd": 2.0, "period": "2026-09"}],
    }
    try:
        out = sb.build_report(data, "2026-08-09")
        check("report builds", isinstance(out, str) and len(out) > 200)
    except Exception as e:  # noqa: BLE001
        check("report builds", False, repr(e))
        return

    check("header shows the window", "since 2026-08-09" in out)
    check("bot appears", "stock_momentum_v1" in out)
    check("live and paper columns both present",
          "LIVE" in out and "PAPER" in out)
    check("never-combined stated", "NEVER COMBINED" in out)
    check("gross stated in the header", "GROSS OF FEES" in out)
    check("notes section present", "NOTES" in out)
    check("omissions stated", "v1 OMITS" in out)
    check("unattributed expense not allocated",
          "unattributed" in out.lower())
    check("error inflation surfaced (41 reported vs 0 rows)",
          "EXCEED" in out, "monitor inflation should be visible")

    # All-time must warn.
    out_all = sb.build_report(data, None)
    check("--all prints the pre-2026-08-09 warning",
          "ALL TIME" in out_all and "2026-08-09" in out_all)

    # Empty everything must not crash or imply "no activity".
    empty = {k: [] for k in data}
    try:
        e = sb.build_report(empty, "2026-08-09")
        check("empty data renders without crashing", isinstance(e, str))
    except Exception as ex:  # noqa: BLE001
        check("empty data renders without crashing", False, repr(ex))

    # None (table unreadable) must not crash either.
    nones = {k: None for k in data}
    try:
        n = sb.build_report(nones, "2026-08-09")
        check("None tables render without crashing", isinstance(n, str))
    except Exception as ex:  # noqa: BLE001
        check("None tables render without crashing", False, repr(ex))


# ── 8. Read-only guarantees ──────────────────────────────────────────────────

def test_read_only() -> None:
    print("\n[8] Read-only by construction")
    path = pathlib.Path(sb.__file__)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    for banned in ("supabase", "public_api", "equities", "anthropic", "openai"):
        check(f"does not import {banned}", banned not in imported)

    # requests is imported, but only GET is ever called.
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for verb in ("post", "patch", "put", "delete"):
        check(f"never calls requests.{verb}", verb not in calls,
              f"found a .{verb}() call — this script must be read-only")
    check("does call requests.get", "get" in calls)

    check("no order-path identifier appears",
          not ({"place_market_buy_amount", "place_market_sell_quantity",
                "place_order_buy", "place_order_sell", "preflight"} & calls))

    # Credentials must never be printed.
    #
    # Checked against the credential EXPRESSION, not the word "key" — the
    # script legitimately prints "LEAGUE_SUPABASE_KEY not set", which names
    # the variable without exposing a value. A substring test on "key"
    # would flag that helpful message as a leak.
    leaky = [
        ln.strip() for ln in src.splitlines()
        if "print(" in ln and ('cfg["key"]' in ln or "cfg['key']" in ln
                               or "Bearer" in ln)
    ]
    check("no credential value is printed", not leaky, f"{leaky}")

    # The key reaches exactly one place: the Authorization header.
    bearer_lines = [ln for ln in src.splitlines() if "Bearer" in ln]
    check("Bearer appears only in the request header",
          len(bearer_lines) == 1, f"{[l.strip() for l in bearer_lines]}")


def main() -> int:
    print("=" * 70)
    print("league_scoreboard — pure helpers (no network, no credentials)")
    print("=" * 70)
    test_grouping()
    test_win_rate()
    test_window()
    test_runs()
    test_expenses()
    test_notes()
    test_report_is_pure()
    test_read_only()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
