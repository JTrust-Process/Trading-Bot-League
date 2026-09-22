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
    test_read_only()
    print("\n" + "=" * 70)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
