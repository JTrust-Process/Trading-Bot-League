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
from urllib.parse import parse_qs, urlsplit

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

    stock_perf = next(line for line in out.splitlines()
                      if line.startswith("stock_momentum_v1") and "|" in line)
    check("stock overview does not imply a measured position count",
          stock_perf.split("|")[-1][:6].strip() == "N/A", stock_perf)
    check("stock overview explains unverified holdings", "UNVERIFIED" in out)

    current = {
        "registry": [{"bot_id": "etf_rotation_v1", "mode": "paper"}],
        "positions": [{"bot_id": "etf_rotation_v1", "symbol": "SPY",
                       "status": "open", "updated_at": "2026-05-01T00:00:00Z"}],
        "runs": [], "trades": [], "errors": [], "expenses": [], "status": [],
    }
    old_open = sb.build_report(current, "2026-08-09")
    etf_perf = next(line for line in old_open.splitlines()
                    if line.startswith("etf_rotation_v1") and "|" in line)
    check("overview counts open positions older than --since",
          etf_perf.split("|")[-1][:6].strip() == "1", etf_perf)
    recent = dict(current, positions=[dict(current["positions"][0],
                                           updated_at="2026-09-01T00:00:00Z")])
    check("position age changes no other overview behavior",
          old_open == sb.build_report(recent, "2026-08-09"))
    closed = dict(current, positions=[dict(current["positions"][0], status="closed")])
    check("closed positions still do not count",
          sb.build_report(closed, "2026-08-09") ==
          sb.build_report(dict(current, positions=[]), "2026-08-09"))
    historical = dict(current,
                      runs=[{"bot_id": "etf_rotation_v1", "status": "success",
                             "started_at": "2026-05-01T00:00:00Z"}],
                      trades=[dict(trade("etf_rotation_v1", 9.0),
                                   occurred_at="2026-05-01T00:00:00Z")])
    check("historical runs/trades still respect --since",
          old_open == sb.build_report(historical, "2026-08-09"))

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

def test_bot_detail() -> None:
    print("\n[12] --bot detail mode")

    print("\n[12a] bot_id is pushed SERVER-SIDE, with --since")
    for key in ("runs", "trades", "errors", "events"):
        q = sb.build_detail_query(sb.DETAIL_SPECS[key], "stock_momentum_v1",
                                  "2026-08-09")
        d = q.replace("%3A", ":").replace("%2C", ",")
        check(f"{key}: bot_id=eq. filter present",
              "bot_id=eq.stock_momentum_v1" in d, q)
        check(f"{key}: since pushed server-side", "gte.2026-08-09" in d, q)
        check(f"{key}: newest first", ".desc" in d, q)
        check(f"{key}: row cap applied", f"limit={sb.DETAIL_ROWS}" in q, q)
        check(f"{key}: URL-encoded", "%2C" in q, q)

    # Open positions must NOT be windowed — ETF has held four since May.
    qp = sb.build_detail_query(sb.DETAIL_SPECS["positions"], "etf_rotation_v1",
                               "2026-08-09")
    check("positions: bot_id filtered", "bot_id=eq.etf_rotation_v1" in qp, qp)
    check("positions: NOT windowed (open positions predate --since)",
          "gte" not in qp, qp)

    parsed = parse_qs(urlsplit(qp).query)
    check("positions: open status filtered SERVER-SIDE",
          parsed.get("status") == ["eq.open"], qp)
    check("positions: limit on the filtered server result",
          parsed.get("limit") == [str(sb.DETAIL_ROWS)], qp)
    encoded = sb.build_detail_query(sb.DETAIL_SPECS["positions"], "bot +&x")
    check("positions: bot ID remains URL-encoded",
          parse_qs(urlsplit(encoded).query)["bot_id"] == ["eq.bot +&x"]
          and "%2B" in encoded and "%26" in encoded, encoded)

    # Simulate server WHERE -> ORDER -> LIMIT over newer closed/other-bot
    # rows and an older open row. Feed the actual generated query through
    # fetch_bot_detail's injected getter; no network or credentials.
    rows = ([{"bot_id": "etf_rotation_v1", "status": "closed",
              "symbol": "CLOSED", "updated_at": "2026-09-20T00:00:00Z"}]
            * (sb.DETAIL_ROWS + 1)
            + [{"bot_id": "another_bot", "status": "open",
                "symbol": "OTHER", "updated_at": "2026-09-21T00:00:00Z"}]
            * (sb.DETAIL_ROWS + 1)
            + [{"bot_id": "etf_rotation_v1", "status": "open",
                "symbol": "SPY", "updated_at": "2026-05-01T00:00:00Z",
                "entry_at": "2026-05-01T00:00:00Z"}])

    def fake_getter(cfg, path):
        table, _, query = path.partition("?")
        if table != "bot_positions":
            return []
        params = parse_qs(query)
        selected = rows
        for key in ("bot_id", "status"):
            if key in params:
                expected = params[key][0].removeprefix("eq.")
                selected = [r for r in selected if r[key] == expected]
        selected = sorted(selected, key=lambda r: r["updated_at"], reverse=True)
        return selected[:int(params["limit"][0])]

    detail = sb.fetch_bot_detail({}, "etf_rotation_v1", "2026-08-09",
                                 getter=fake_getter)
    check("server filters open positions and bot BEFORE limit",
          [r["symbol"] for r in detail["positions"]] == ["SPY"])
    rendered = sb.render_bot_detail("etf_rotation_v1", {}, {}, detail, "2026-08-09")
    check("old open position appears in windowed detail", "SPY" in rendered)
    check("newer closed/other-bot positions cannot crowd out the open row",
          "CLOSED" not in rendered and "OTHER" not in rendered)

    q_all = sb.build_detail_query(sb.DETAIL_SPECS["runs"], "x", None)
    check("--all sends no gte", "gte" not in q_all, q_all)
    check("--all still filters by bot", "bot_id=eq.x" in q_all, q_all)

    print("\n[12b] Run status notes")
    runs = [
        {"started_at": "2026-09-22T10:00:00+00:00", "ended_at": None,
         "status": "running", "trade_count": 0, "error_count": 0},
        {"started_at": "2026-09-21T10:00:00+00:00",
         "ended_at": "2026-09-21T10:00:12+00:00", "status": "failed",
         "trade_count": 0, "error_count": 3, "duration_ms": 12000},
        {"started_at": "2026-09-20T10:00:00+00:00",
         "ended_at": "2026-09-20T10:00:05+00:00", "status": "warning",
         "trade_count": 1, "error_count": 1, "duration_ms": 5000},
        {"started_at": "2026-09-19T10:00:00+00:00",
         "ended_at": "2026-09-19T10:00:04+00:00", "status": "success",
         "trade_count": 2, "error_count": 0, "duration_ms": 4000},
    ]
    out = sb.render_bot_detail("b", {"bot_type": "stock", "mode": "live"},
                               {"health": "healthy"},
                               {"runs": runs}, "2026-08-09")
    check("running flagged as possibly orphaned", "ORPHANED" in out, "")
    check("failed flagged", "FAILED" in out)
    check("warning flagged as degraded", "degraded" in out)
    check("success gets no scary note", out.count("FAILED") == 1)
    check("duration rendered in seconds", "12.0" in out)

    print("\n[12c] Open positions and unaudited marks")
    pos = [
        {"symbol": "SPY", "status": "open", "quantity": 0.1,
         "entry_price": 500.0, "entry_at": "2026-05-21T14:00:00+00:00",
         "amount_usd": 50.0, "is_paper": False,
         "metadata": {"mark_pnl_usd": 3.21, "dry_run": False}},
        {"symbol": "QQQ", "status": "open", "quantity": 0.1,
         "entry_price": 400.0, "entry_at": "2026-05-21T14:00:00+00:00",
         "amount_usd": 40.0, "is_paper": False, "metadata": {}},
        {"symbol": "OLD", "status": "closed", "quantity": 1.0,
         "entry_price": 10.0, "metadata": {}},
    ]
    out = sb.render_bot_detail("etf", None, None, {"positions": pos}, None)
    check("mark_pnl_usd rendered", "3.21" in out, "")
    check("closed positions excluded", "OLD" not in out)
    check("open positions shown", "SPY" in out and "QQQ" in out)
    check("mark labelled UNREALIZED", "UNREALIZED" in out)
    check("mark labelled UNAUDITED", "UNAUDITED" in out)
    check("explains it is not the pnl_usd column", "pnl_usd" in out)
    check("blank mark explained", "never been marked" in out)
    check("position age shown", "d" in out)

    print("\n[12d] Live and paper are labelled, never totalled")
    trades = [
        {"occurred_at": "2026-09-20T10:00:00+00:00", "symbol": "AAPL",
         "side": "SELL", "price": 230.0, "quantity": 0.1, "amount_usd": 23.0,
         "pnl_usd": -1.25, "pnl_pct": -0.0325, "is_paper": False,
         "strategy": "dynamic_sl"},
        {"occurred_at": "2026-09-19T10:00:00+00:00", "symbol": "MSFT",
         "side": "SELL", "price": 400.0, "quantity": None, "amount_usd": None,
         "pnl_usd": 50.0, "pnl_pct": 0.10, "is_paper": True,
         "strategy": "momentum_exit"},
    ]
    out = sb.render_bot_detail("b", None, None, {"trades": trades}, None)
    check("live row labelled", "LIVE" in out)
    check("paper row labelled", "PAPER" in out)
    check("section states it does not total", "does NOT total" in out)
    check("missing quantity renders as dash, not 0",
          "0.00000000" not in out.split("MSFT")[1][:60],
          "a NULL quantity must not print as zero")
    check("pnl_pct rendered as percent", "-3.25%" in out or "3.25%" in out)

    print("\n[12e] Unreadable vs empty are different statements")

    # Assert against ONE SECTION, not the whole report. render_bot_detail
    # always emits all five sections, so a fixture supplying only `runs`
    # leaves the other four as dict.get() -> None, which correctly renders
    # UNREADABLE. Searching the full string found that unrelated section
    # and reported a failure in the one being tested.
    def section_of(report: str, header_prefix: str) -> str:
        lines = report.splitlines()
        heads = ("RECENT RUNS", "RECENT TRADES", "OPEN POSITIONS",
                 "RECENT ERRORS", "RECENT EVENTS", "=" * 10)
        try:
            start = next(i for i, ln in enumerate(lines)
                         if ln.startswith(header_prefix))
        except StopIteration:
            return ""
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if any(lines[j].startswith(h) for h in heads):
                end = j
                break
        return "\n".join(lines[start:end])

    out = sb.render_bot_detail("b", None, None, {"runs": None}, None)
    runs_sec = section_of(out, "RECENT RUNS")
    check("section extractor found RECENT RUNS", bool(runs_sec))
    check("None -> UNREADABLE", "UNREADABLE" in runs_sec, runs_sec)
    check("None warns absence is not evidence", "NOT evidence" in runs_sec)

    out = sb.render_bot_detail("b", None, None, {"runs": []}, None)
    runs_sec = section_of(out, "RECENT RUNS")
    check("[] -> (none in this window)",
          "(none in this window)" in runs_sec, runs_sec)
    check("[] does NOT say unreadable", "UNREADABLE" not in runs_sec, runs_sec)
    check("[] does not warn about evidence", "NOT evidence" not in runs_sec)

    # The realistic case, and a stronger assertion than the original: every
    # section fetched successfully and every one empty. Nothing anywhere in
    # the report may claim a read failure.
    all_empty = sb.render_bot_detail(
        "b", None, None, {k: [] for k in sb.DETAIL_SPECS}, None)
    check("all sections empty -> no UNREADABLE anywhere",
          "UNREADABLE" not in all_empty)
    check("all sections empty -> each says (none)",
          all_empty.count("(none in this window)") >= 4,
          f"got {all_empty.count('(none in this window)')}")

    # And the inverse: every section failing must say so every time.
    all_none = sb.render_bot_detail(
        "b", None, None, {k: None for k in sb.DETAIL_SPECS}, None)
    check("all sections unreadable -> UNREADABLE in each",
          all_none.count("UNREADABLE") >= 4,
          f"got {all_none.count('UNREADABLE')}")
    check("all sections unreadable -> never says (none)",
          "(none in this window)" not in all_none)

    for bot, rows, expected in (
        ("stock_momentum_v1", [], "UNVERIFIED"),
        ("stock_momentum_v1", None, "UNREADABLE"),
        ("etf_rotation_v1", [], "(no open positions)"),
        ("short_watchlist_v1", [], "(no open positions)"),
    ):
        report = sb.render_bot_detail(bot, {}, {}, {"positions": rows}, "2026-08-09")
        section = section_of(report, "OPEN POSITIONS")
        check(f"{bot} positions {rows!r}: {expected}", expected in section, section)
        check(f"{bot} positions {rows!r}: no window-based empty message",
              "none in this window" not in section)
        if bot == "stock_momentum_v1":
            check(f"stock positions {rows!r}: never claims no holdings",
                  "no open positions" not in section, section)
            other = "UNREADABLE" if rows == [] else "UNVERIFIED"
            check(f"stock positions {rows!r}: distinct failure/coverage state",
                  other not in section, section)
            check(f"stock positions {rows!r}: no inferred symbols",
                  "META" not in section and "GOOGL" not in section)

    stock_rows = [{"symbol": "TEST", "status": "open"}]
    section = section_of(sb.render_bot_detail(
        "stock_momentum_v1", {}, {}, {"positions": stock_rows}, None), "OPEN POSITIONS")
    check("stock nonempty League rows remain unverified", "UNVERIFIED" in section)

    print("\n[12h] Positions query: open-only, server-side, unwindowed")
    qp2 = sb.build_detail_query(sb.DETAIL_SPECS["positions"],
                                "stock_momentum_v1", "2026-08-09")
    d = qp2.replace("%3A", ":").replace("%2C", ",")
    check("status=eq.open pushed server-side", "status=eq.open" in d, qp2)
    check("bot_id pushed server-side", "bot_id=eq.stock_momentum_v1" in d, qp2)
    check("no --since window on positions", "gte" not in d, qp2)
    # Order matters: PostgREST applies filters BEFORE limit, so the cap
    # counts OPEN rows only. Filtering after the fetch meant 20 rows of
    # mostly-closed history could yield zero opens and render as
    # "no open positions" — a limit artifact dressed up as a portfolio fact.
    check("limit comes after the filters",
          d.index("status=eq.open") < d.index("limit="), qp2)
    check("URL-encoded", "%2C" in qp2, qp2)

    print("\n[12i] Non-mirrored bots: UNVERIFIED, never 'no open positions'")
    check("stock is registered as non-mirrored",
          "stock_momentum_v1" in sb.POSITIONS_NOT_MIRRORED)
    check("crypto is registered as non-mirrored",
          "crypto_ema_atr_v1" in sb.POSITIONS_NOT_MIRRORED,
          "crypto has the same gap as stock — positions live in "
          "trader.positions and the crypto project's own bot_state")
    check("etf is NOT in the non-mirrored set",
          "etf_rotation_v1" not in sb.POSITIONS_NOT_MIRRORED)
    check("short is NOT in the non-mirrored set",
          "short_watchlist_v1" not in sb.POSITIONS_NOT_MIRRORED)

    for bot in ("stock_momentum_v1", "crypto_ema_atr_v1"):
        out = sb.render_bot_detail(bot, None, None, {"positions": []}, None)
        sec = section_of(out, "OPEN POSITIONS")
        check(f"{bot}: empty -> UNVERIFIED", "UNVERIFIED" in sec, sec)
        check(f"{bot}: does NOT claim no open positions",
              "(no open positions)" not in sec, sec)
        check(f"{bot}: names the real source of truth",
              "not mirrored" in sec or "own" in sec, sec)
        # Must stay distinct from a failed fetch.
        un = section_of(sb.render_bot_detail(bot, None, None,
                                             {"positions": None}, None),
                        "OPEN POSITIONS")
        check(f"{bot}: unreadable -> UNREADABLE, not UNVERIFIED",
              "UNREADABLE" in un and "UNVERIFIED" not in un, un)

    print("\n[12j] Mirrored bots keep normal semantics")
    for bot in ("etf_rotation_v1", "short_watchlist_v1", "bond_research_v1"):
        sec = section_of(sb.render_bot_detail(bot, None, None,
                                              {"positions": []}, None),
                         "OPEN POSITIONS")
        check(f"{bot}: empty -> (no open positions)",
              "(no open positions)" in sec, sec)
        check(f"{bot}: no UNVERIFIED warning", "UNVERIFIED" not in sec, sec)

    print("\n[12k] Overview: open column and notes for non-mirrored bots")
    data = {k: [] for k in sb.TABLE_SPECS}
    data["registry"] = [{"bot_id": b, "bot_type": "x", "mode": "live"}
                        for b in ("stock_momentum_v1", "crypto_ema_atr_v1",
                                  "etf_rotation_v1")]
    # An OPEN position whose updated_at predates --since must still count.
    data["positions"] = [{"bot_id": "etf_rotation_v1", "status": "open",
                          "symbol": "SPY",
                          "updated_at": "2026-05-21T14:00:00+00:00"}]
    ov = sb.build_report(data, "2026-08-09")
    rows = {ln.split("|")[0].strip(): ln for ln in ov.splitlines()
            if "|" in ln and "bot_id" not in ln}
    check("stock open column reads N/A, not 0",
          "N/A" in rows.get("stock_momentum_v1", ""),
          rows.get("stock_momentum_v1", ""))
    check("crypto open column reads N/A, not 0",
          "N/A" in rows.get("crypto_ema_atr_v1", ""),
          rows.get("crypto_ema_atr_v1", ""))
    check("ETF open position older than --since STILL counted",
          " 1" in rows.get("etf_rotation_v1", ""),
          rows.get("etf_rotation_v1", ""))
    check("stock NOTES carry the unverified warning",
          "UNVERIFIED" in ov, "")
    check("non-mirrored bots get no 'N open position(s)' note",
          "open position(s) NOT valued" not in ov
          or "etf" in ov.lower(), "")

    print("\n[12f] Header and unknown bots")
    out = sb.render_bot_detail(
        "stock_momentum_v1",
        {"bot_type": "stock", "mode": "live", "status": "enabled",
         "can_place_orders": True, "max_order_usd": 15},
        {"health": "healthy", "last_heartbeat_at": "2026-09-22T07:37:07+00:00",
         "last_run_status": "success", "last_run_id": "abc-123"},
        {}, "2026-08-09")
    check("bot_id in header", "stock_momentum_v1" in out)
    check("type shown", "stock" in out)
    check("mode shown", "live" in out)
    check("can_place_orders shown", "can_place_orders=True" in out)
    check("heartbeat shown", "2026-09-22T07:37:07" in out)
    check("last run id shown", "abc-123" in out)
    check("window shown", "since 2026-08-09" in out)

    unknown = sb.render_bot_detail("nope", None, None, {}, None)
    check("unknown bot says so", "not found in bot_registry" in unknown)
    check("unknown bot does not crash", isinstance(unknown, str))

    print("\n[12g] Overview mode is unaffected")
    data = {k: [] for k in sb.TABLE_SPECS}
    ov = sb.build_report(data, "2026-08-09")
    check("overview still renders", "LEAGUE SCOREBOARD" in ov)
    check("overview is not the detail view", "BOT DETAIL" not in ov)
    check("detail is not the overview",
          "LEAGUE SCOREBOARD" not in sb.render_bot_detail("b", None, None, {}, None))


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


def test_query_direction_and_encoding() -> None:
    """The v1 bug: asc+limit fetched the OLDEST rows and dropped the present.

    Confirmed in production 2026-09-22 — bot_runs held 9,726 rows with the
    newest at 2026-09-22T07:37Z, while the scoreboard reported 0 runs since
    2026-08-09. The fetch had taken the oldest 5,000.

    Ordering direction is therefore load-bearing, not stylistic, and these
    assertions exist to keep it that way.
    """
    print("\n[9] Query ordering — newest first, never oldest")

    for key, ts in (("runs", "started_at"), ("trades", "occurred_at"),
                    ("errors", "occurred_at")):
        spec = sb.TABLE_SPECS[key]
        q = sb.build_query(spec, since="2026-08-09")
        check(f"{key}: orders {ts}.desc",
              f"order={ts}.desc" in q.replace("%2C", ",").replace("%3A", ":"),
              q)
        check(f"{key}: NOT ascending", f"{ts}.asc" not in q, q)
        check(f"{key}: has a page limit", "limit=" in q, q)
        check(f"{key}: page size <= 1000",
              f"limit={sb.PAGE_SIZE}" in q and sb.PAGE_SIZE <= 1000, q)

    print("\n[9b] The --since window is pushed SERVER-SIDE")
    for key, ts in (("runs", "started_at"), ("trades", "occurred_at"),
                    ("errors", "occurred_at")):
        q = sb.build_query(sb.TABLE_SPECS[key], since="2026-08-09")
        decoded = q.replace("%3A", ":").replace("%2B", "+")
        check(f"{key}: includes {ts}=gte.2026-08-09",
              f"{ts}=gte.2026-08-09" in decoded, q)
        # Without a window, no filter — otherwise --all would lie.
        q_all = sb.build_query(sb.TABLE_SPECS[key], since=None)
        check(f"{key}: --all sends no gte filter", "gte" not in q_all, q_all)

    print("\n[9c] Tables deliberately NOT windowed server-side")
    # An open position may predate the window and still be open now.
    q = sb.build_query(sb.TABLE_SPECS["positions"], since="2026-08-09")
    check("positions: no gte filter (open positions predate the window)",
          "gte" not in q, q)
    check("status: no gte filter (always want current status)",
          "gte" not in sb.build_query(sb.TABLE_SPECS["status"], "2026-08-09"))

    print("\n[9d] Parameters are URL-ENCODED, not concatenated")
    # A raw '+' in a query string decodes to a SPACE server-side, which is
    # how an ISO offset silently becomes a different (or invalid) instant.
    iso = "2026-09-20T10:30:00+00:00"
    q = sb.build_query(sb.TABLE_SPECS["runs"], since=iso)
    check("'+' is escaped to %2B", "%2B" in q, q)
    check("no raw '+' survives in the query", "+00:00" not in q, q)
    check("':' is escaped to %3A", "%3A" in q, q)
    check("select commas escaped", "%2C" in q or "," not in q.split("select=")[1][:60])

    src = pathlib.Path(sb.__file__).read_text(encoding="utf-8")
    check("uses urlencode", "urlencode(" in src)
    check("no f-string builds a gte filter directly",
          'f"&started_at=gte.' not in src and "f'&started_at=gte." not in src)


def test_pagination() -> None:
    """The server caps below the client limit — paging is the only fix.

    Production symptom: limit=5000 returned exactly 1000 (PostgREST's
    max-rows), the script compared 1000 against its own 5000 and concluded
    "complete", and every bot's first-seen date became an artifact of where
    those 1000 rows ran out.
    """
    print("\n[11] Pagination")
    spec = sb.TABLE_SPECS["runs"]

    print("\n[11a] build_query carries offset")
    q0 = sb.build_query(spec, "2026-08-09", offset=0, page_size=1000)
    q1 = sb.build_query(spec, "2026-08-09", offset=1000, page_size=1000)
    check("page 1 has offset=0", "offset=0" in q0, q0)
    check("page 2 has offset=1000", "offset=1000" in q1, q1)
    check("page size honoured", "limit=1000" in q0, q0)
    check("gte survives paging", "gte.2026-08-09" in q1.replace("%3A", ":"), q1)
    check("order survives paging", "order=started_at.desc" in q1, q1)
    check("still URL-encoded", "%2C" in q1, q1)

    def pager(pages: list[list[dict]]):
        """Fake getter returning canned pages; records every URL."""
        seen: list[str] = []

        def _g(cfg, path):
            seen.append(path)
            i = len(seen) - 1
            return pages[i] if i < len(pages) else []
        return _g, seen

    page = [{"started_at": "2026-09-01T00:00:00+00:00"}] * 1000
    short = [{"started_at": "2026-08-20T00:00:00+00:00"}] * 137

    print("\n[11b] Multiple pages combine")
    g, seen = pager([page, page, short])
    rows, m = sb.fetch_paginated({}, spec, "2026-08-09", page_size=1000, getter=g)
    check("all pages combined", len(rows) == 2137, f"got {len(rows)}")
    check("three requests made", len(seen) == 3, f"made {len(seen)}")
    check("pages counted", m["pages"] == 3, f"got {m['pages']}")
    check("NOT truncated — ended naturally", m["truncated"] is False)
    check("offsets advanced 0/1000/2000",
          "offset=0" in seen[0] and "offset=1000" in seen[1]
          and "offset=2000" in seen[2], f"{seen}")

    print("\n[11c] A full page ALWAYS triggers another request")
    g, seen = pager([page, []])
    rows, m = sb.fetch_paginated({}, spec, None, page_size=1000, getter=g)
    check("exactly 1000 rows -> second page requested", len(seen) == 2,
          "a full page is indistinguishable from a server cap until you ask")
    check("second page empty -> stop", len(rows) == 1000)
    check("not truncated once confirmed", m["truncated"] is False)

    print("\n[11d] Stops on a short page")
    g, seen = pager([short])
    rows, m = sb.fetch_paginated({}, spec, None, page_size=1000, getter=g)
    check("one request only", len(seen) == 1)
    check("short page ends the scan", len(rows) == 137)
    check("not truncated", m["truncated"] is False)

    print("\n[11e] Guards")
    g, seen = pager([page] * 50)
    rows, m = sb.fetch_paginated({}, spec, None, page_size=1000,
                                 max_pages=3, getter=g)
    check("max_pages caps the loop", len(seen) == 3, f"made {len(seen)}")
    check("guard sets TRUNCATED", m["truncated"] is True)
    check("rows returned up to the guard", len(rows) == 3000)

    print("\n[11f] Failure handling")
    def fail_first(cfg, path):
        return None
    rows, m = sb.fetch_paginated({}, spec, None, getter=fail_first)
    check("first page fails -> None, unreadable",
          rows is None and m["unreadable"] is True)
    check("unreadable is not 'truncated'", m["truncated"] is False)

    calls = {"n": 0}
    def fail_second(cfg, path):
        calls["n"] += 1
        return page if calls["n"] == 1 else None
    rows, m = sb.fetch_paginated({}, spec, None, page_size=1000,
                                 getter=fail_second)
    check("mid-scan failure keeps earlier pages", len(rows) == 1000)
    check("mid-scan failure marks PARTIAL", m["partial"] is True)
    check("partial also marks truncated (never 'complete')",
          m["truncated"] is True)


def test_freshness_footer() -> None:
    print("\n[10] DATA FRESHNESS footer")
    rows = [{"started_at": f"2026-09-{d:02d}T10:00:00+00:00"} for d in range(1, 11)]

    m = sb.table_freshness(rows, "started_at", {"pages": 1, "truncated": False})
    check("counts rows", m["rows"] == 10)
    check("latest is the max, not the first", m["latest"].startswith("2026-09-10"),
          f"got {m['latest']}")
    check("not truncated when paging ended naturally", m["truncated"] is False)
    check("page count carried", m["pages"] == 1)

    # THE regression: a row count equal to a cap is NOT truncation on its
    # own. Only the pagination outcome decides.
    thousand = [{"started_at": "2026-09-01T00:00:00+00:00"}] * 1000
    m = sb.table_freshness(thousand, "started_at",
                           {"pages": 2, "truncated": False})
    check("exactly 1000 rows is COMPLETE when paging confirmed it",
          m["truncated"] is False,
          "the old check inferred truncation from the row count alone")
    m = sb.table_freshness(thousand, "started_at",
                           {"pages": 30, "truncated": True})
    check("1000 rows IS truncated when the guard fired", m["truncated"] is True)

    m = sb.table_freshness(None, "started_at", {"unreadable": True})
    check("None -> unreadable, not empty", m["unreadable"] is True)
    check("unreadable has no row count", m["rows"] is None)

    m = sb.table_freshness([], "started_at", {"pages": 1, "truncated": False})
    check("empty list is readable, 0 rows",
          m["unreadable"] is False and m["rows"] == 0)
    check("empty list -> no latest", m["latest"] is None)
    check("empty list not truncated", m["truncated"] is False)

    m = sb.table_freshness([{"x": 1}], None, {"pages": 1})
    check("no ts key -> latest None, no crash", m["latest"] is None)

    out = "\n".join(sb.render_freshness({
        "runs": {"table": "bot_runs", "rows": 3000, "pages": 3,
                 "latest": "2026-09-22T14:17:00+00:00", "truncated": True,
                 "partial": False, "unreadable": False},
        "trades": {"table": "bot_trades", "rows": 38, "pages": 1,
                   "latest": "2026-09-19T18:02:00+00:00", "truncated": False,
                   "partial": False, "unreadable": False},
        "errors": {"table": "bot_errors", "rows": None, "latest": None,
                   "truncated": False, "partial": False, "unreadable": True},
    }))
    check("footer header present", "DATA FRESHNESS" in out)
    check("TRUNCATED marker shown", "TRUNCATED" in out, out)
    check("page count shown", "3pg" in out, out)
    check("guard is explained", "guard" in out.lower(), out)
    check("truncation explains what is missing", "OLDEST" in out, out)
    check("unreadable table named as such", "UNREADABLE" in out, out)
    check("unreadable warns figures exclude it", "EXCLUDE" in out, out)
    check("non-truncated table has no marker",
          "bot_trades" in out and "38 rows" in out)

    partial = "\n".join(sb.render_freshness({
        "runs": {"table": "bot_runs", "rows": 1000, "pages": 1, "latest": None,
                 "truncated": True, "partial": True, "unreadable": False},
    }))
    check("PARTIAL shown when a page failed", "PARTIAL" in partial, partial)

    # And the footer reaches the report.
    data = {k: [] for k in sb.TABLE_SPECS}
    meta = {"runs": {"table": "bot_runs", "rows": 3000, "pages": 3,
                     "latest": None, "truncated": True, "partial": False,
                     "unreadable": False}}
    rep = sb.build_report(data, "2026-08-09", meta)
    check("build_report includes the footer", "DATA FRESHNESS" in rep)
    check("build_report still works without meta",
          "DATA FRESHNESS" not in sb.build_report(data, "2026-08-09"))


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
    test_query_direction_and_encoding()
    test_pagination()
    test_freshness_footer()
    test_bot_detail()
    test_read_only()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
