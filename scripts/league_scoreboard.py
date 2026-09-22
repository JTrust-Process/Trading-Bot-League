"""league_scoreboard — read-only scoreboard for the Trading Bot League.

    python -m scripts.league_scoreboard
    python -m scripts.league_scoreboard --all
    python -m scripts.league_scoreboard --since 2026-09-01

READ-ONLY. GET requests to the League Supabase project only. It places no
orders, touches no broker, reads no market data, and writes nothing
anywhere. It cannot affect a trading cycle.

────────────────────────────────────────────────────────────────────────────
WHAT THIS IS FOR, AND THE ONE THING IT MUST NOT DO

The purpose is to replace a feeling ("crypto seems fine", "the stock bot is
bleeding") with a number. The failure mode is producing a number that looks
authoritative and isn't — which would be worse than the feeling, because a
feeling knows it is a feeling.

So every figure here ships with its caveat attached, in the NOTES section,
not in a footnote someone skips:

  * P/L IS GROSS OF FEES. bot_trades.fees_usd defaults to 0 and neither
    live bot passes it. Crypto's real cost is ~$0.06 per leg on $10-25
    positions, which is the same order of magnitude as its entire lifetime
    result. A "roughly break-even" crypto reading may be a structurally
    negative one.

  * LIVE AND PAPER ARE NEVER SUMMED. They are different claims about
    reality. A combined number is meaningless.

  * n IS ALWAYS SHOWN. At n=18 you cannot distinguish a 39% win rate from
    the 46% needed to break even. Any bot under 30 closed trades is
    flagged; the flag is not a hedge, it is the statistical situation.

  * THE DEFAULT WINDOW STARTS 2026-08-09. Before that date stop-losses were
    disabled in five distinct ways, kill-switch exits skipped their
    bookkeeping, and the stock monitor inflated error counts across runs.
    All-time figures measure a system that no longer exists. --all exists,
    and prints a warning.

────────────────────────────────────────────────────────────────────────────
WHAT IT DELIBERATELY OMITS (v1)

  * Unrealized P/L on open positions. bot_positions.pnl_usd is only
    populated at close; ETF's marks live in metadata. Open positions are
    COUNTED, never valued.
  * Benchmarks (SPY/QQQ) and any market-data call.
  * Time-weighted return, drawdown, Sharpe. All need an equity curve, and
    nothing records account equity over time. That is the single highest
    -value future addition; per-trade P/L cannot substitute for it.

Structure: pure helpers first (no IO, unit-tested by
scripts/_scoreboard_smoke.py), then the read layer, then rendering.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


#: Everything before this is a different system. See the module docstring.
DEFAULT_SINCE = "2026-08-09"

#: Below this, an average is an anecdote.
MIN_MEANINGFUL_N = 30

STOCK_POSITION_WARNING = (
    "Stock position state is not mirrored into League bot_positions; "
    "holdings are UNVERIFIED in this report."
)

CRYPTO_POSITION_WARNING = (
    "Crypto position state is not mirrored into League bot_positions; "
    "holdings are UNVERIFIED in this report."
)


#: Per-bot data-quality gaps, from the 2026-09 audit. Surfaced in NOTES so
#: nobody reads a column that is structurally empty as a real zero.
KNOWN_GAPS: dict[str, tuple[str, ...]] = {
    "stock_momentum_v1": (
        STOCK_POSITION_WARNING + " Overview open=N/A means unverified holdings.",
        "bot_trades rows carry no quantity/order_id/run_id "
        "(league_status.log_trade call omits them) -> cannot reconcile "
        "against Public, and the (bot_id, order_id) dedup index is inert",
        "run error_count before the 2026-09-22 monitor fix is INFLATED: the "
        "singleton accumulated across runs in one long-lived process",
    ),
    "crypto_ema_atr_v1": (
        CRYPTO_POSITION_WARNING + " Overview open=N/A means unverified holdings.",
        "bot_trades rows carry no amount_usd/pnl_pct",
        "fees ~$0.06/leg are NOT recorded; on $10-25 positions that is the "
        "same magnitude as the result itself",
        "historical rows include DRY_RUN entries; separated only by is_paper",
    ),
    "etf_rotation_v1": (
        "unrealized P/L lives in bot_positions.metadata.mark_pnl_usd, not "
        "the pnl_usd column -> a flat reading here may not mean flat",
        "was live with can_place_orders=false and an unreachable order size "
        "for ~1 month; zero fills in that period is a config artifact",
    ),
    "short_watchlist_v1": (
        "paper only; no capital was ever at risk",
    ),
    "agent_research_v1": (
        "research bot: produces signals/approvals, never trades",
    ),
    "bond_research_v1": (
        "research bot: produces scores, never trades",
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
#  QUERY SPECS — one source of truth for what is fetched and how.
# ═══════════════════════════════════════════════════════════════════════════
#
# ⚠ ORDER DIRECTION IS LOAD-BEARING. Every ordered fetch MUST be `.desc`.
#
# v1 used `order=started_at.asc&limit=5000`, which returns the 5,000
# OLDEST rows. Crypto alone writes ~96 runs/day (every 15 min, 24/7), so
# the cap was reached roughly six weeks in and every newer run was
# discarded BEFORE the --since window was applied. The scoreboard then
# reported 0 recent runs while bot_status and bot_positions — small tables
# nowhere near any limit — looked perfectly current.
#
# With `.desc`, a limit truncates the OLDEST rows instead: you lose
# history you were not asking for, never the present.
#
# This is the same defect flagged in the 2026-09 Public API audit
# (Polygon called with `sort=asc&limit=200` over a ~205-day range, silently
# dropping the newest bars). Writing it into this script three weeks later
# is why the DATA FRESHNESS footer below now exists: a TRUNCATED marker
# makes the failure self-diagnosing instead of invisible.

# ── Pagination ──────────────────────────────────────────────────────────────
#
# PostgREST enforces its OWN row cap (Supabase defaults to 1000) regardless
# of the `limit` a client asks for. The first version of this script sent
# limit=5000, got exactly 1000 back, and reported that as complete — its
# truncation check compared the result against the CLIENT limit and never
# saw the server's.
#
# The symptom in production: every bot's "first run" showed as 2026-09-12
# or later even though the window opened 2026-08-09, because that is simply
# where the newest 1000 rows ran out. Run counts, first-seen dates and
# error totals were all understated, and nothing in the output said so.
#
# So: page through with offsets until a page comes back short. Truncation
# now means "the SAFETY GUARD stopped us", never "we hit some cap and
# assumed that was everything".

PAGE_SIZE = 1000          # must be <= PostgREST's max-rows
MAX_PAGES = 30            # guard: a broken query must not loop forever
MAX_ROWS = PAGE_SIZE * MAX_PAGES


TABLE_SPECS: dict[str, dict[str, Any]] = {
    # NOTE: every spec carries an `order`. Offset pagination without a
    # stable sort is non-deterministic — rows can repeat or vanish between
    # pages — so even tables that do not need sorting for display get one.
    "registry": {
        "table": "bot_registry", "select": "*",
        "ts": None, "order": "bot_id.asc", "since_key": None,
    },
    "status": {
        "table": "bot_status", "select": "*",
        "ts": "last_heartbeat_at", "order": "last_heartbeat_at.desc",
        "since_key": None,   # always want current status, unwindowed
    },
    "runs": {
        "table": "bot_runs",
        "select": "bot_id,started_at,status,trade_count,error_count",
        "ts": "started_at", "order": "started_at.desc",
        "since_key": "started_at",
    },
    "trades": {
        "table": "bot_trades",
        "select": "bot_id,occurred_at,side,pnl_usd,is_paper,strategy,symbol",
        "ts": "occurred_at", "order": "occurred_at.desc",
        "since_key": "occurred_at",
    },
    "positions": {
        "table": "bot_positions",
        "select": "bot_id,status,symbol,updated_at",
        "ts": "updated_at", "order": "updated_at.desc",
        # No since_key: an OPEN position may have been entered long before
        # the window and is still open now. Filtering it out server-side
        # would undercount current exposure.
        "since_key": None,
    },
    "errors": {
        "table": "bot_errors", "select": "bot_id,occurred_at",
        "ts": "occurred_at", "order": "occurred_at.desc",
        "since_key": "occurred_at",
    },
    "expenses": {
        "table": "bot_expenses",
        # created_at is selected purely so DATA FRESHNESS can report a
        # latest timestamp. Without it the footer always showed "—",
        # which reads as "no data" rather than "column not fetched".
        "select": "bot_id,amount_usd,period,category,recurring,created_at",
        # Windowed client-side by `period` (YYYY-MM), not by a timestamp.
        "ts": "created_at", "order": "period.desc", "since_key": None,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  PURE HELPERS — no IO, no network. Unit-tested.
# ═══════════════════════════════════════════════════════════════════════════


def build_query(spec: dict[str, Any], since: Optional[str] = None, *,
                offset: int = 0, page_size: int = PAGE_SIZE) -> str:
    """Build one PostgREST path. Pure — no IO. URL-ENCODED.

    Encoding is not cosmetic. A raw `+` in a query string decodes to a
    SPACE, so interpolating an ISO timestamp like
    '2026-09-20T10:30:00+00:00' directly produces
    'lt.2026-09-20T10:30:00 00:00' server-side — a parse error or, worse,
    a silently different instant. urlencode escapes `+` and `:` correctly.

    Pushing the window into the query (rather than filtering after) is what
    makes the row limit safe: the cap then applies to the window you asked
    for instead of to all history.
    """
    params: list[tuple[str, str]] = [("select", spec["select"])]
    since_key = spec.get("since_key")
    if since_key and since:
        params.append((since_key, f"gte.{since}"))
    if spec.get("order"):
        params.append(("order", spec["order"]))
    params.append(("limit", str(page_size)))
    params.append(("offset", str(int(offset))))
    return f"{spec['table']}?{urlencode(params)}"


def table_freshness(
    rows: Optional[list[dict[str, Any]]],
    ts_key: Optional[str],
    page_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Row count, newest timestamp, and pagination outcome.

    `truncated` comes from PAGINATION, not from comparing the row count to
    a limit. That distinction is the whole fix: the previous version
    inferred completeness from "fewer rows than I asked for", which is
    false whenever the SERVER caps below the client's limit. Exactly 1000
    rows is now only ever considered complete if a further page was
    requested and came back short.
    """
    if rows is None:
        return {"rows": None, "latest": None, "truncated": False,
                "pages": 0, "unreadable": True, "partial": False}
    latest: Optional[str] = None
    if ts_key:
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get(ts_key)
            if isinstance(v, str) and v and (latest is None or v > latest):
                latest = v
    pm = page_meta or {}
    return {
        "rows": len(rows),
        "latest": latest,
        "pages": pm.get("pages", 1),
        # True ONLY when the safety guard stopped us, or a page failed
        # mid-scan. Never "we hit a cap and assumed that was everything".
        "truncated": bool(pm.get("truncated")),
        "partial": bool(pm.get("partial")),
        "unreadable": False,
    }


def fetch_paginated(
    cfg: dict[str, str],
    spec: dict[str, Any],
    since: Optional[str] = None,
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    getter: Any = None,
) -> tuple[Optional[list[dict[str, Any]]], dict[str, Any]]:
    """Page through one table with GET requests. Returns (rows, page_meta).

    Stops when a page returns FEWER rows than page_size — the only reliable
    signal that the end was reached. A page returning exactly page_size
    always triggers another request, because that is indistinguishable from
    a server-side cap until you ask again.

    `getter` is injected so the pagination logic can be unit-tested with no
    network. It defaults to the real GET.

    Guards: max_pages bounds the loop, and a mid-scan failure stops cleanly
    and is reported as `partial` rather than silently returning a short
    list that would read as complete.
    """
    fetch = getter or _get
    rows: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    partial = False

    while True:
        if pages >= max_pages:
            truncated = True
            break
        page = fetch(cfg, build_query(spec, since,
                                      offset=len(rows), page_size=page_size))
        if page is None:
            if pages == 0:
                return None, {"pages": 0, "truncated": False,
                              "partial": False, "unreadable": True}
            # Some pages already succeeded: keep them, but never present
            # the result as complete.
            partial = True
            truncated = True
            break
        pages += 1
        rows.extend(page)
        if len(page) < page_size:
            break          # short page == genuinely the end
        if len(rows) >= MAX_ROWS:
            truncated = True
            break

    return rows, {"pages": pages, "truncated": truncated, "partial": partial,
                  "unreadable": False}


def parse_since(value: Optional[str], *, all_time: bool = False) -> Optional[str]:
    """Return an ISO date string, or None for all-time.

    Raises ValueError on a malformed date rather than silently falling back
    to the default — a scoreboard that quietly reports a different window
    than the one you asked for is exactly the class of bug this tool exists
    to expose.
    """
    if all_time:
        return None

    # NOT `value or DEFAULT_SINCE`. That was the original form and it had a
    # bug: "" is falsy, so `--since ""` silently fell through to the
    # default and the report covered a window nobody asked for — while its
    # own header confidently announced that window as the requested one.
    #
    # Only a genuinely ABSENT value (None) takes the default. Anything the
    # caller actually supplied must parse, including "" and "   ".
    if value is None:
        raw = DEFAULT_SINCE
    else:
        raw = str(value).strip()

    try:
        date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"--since must be YYYY-MM-DD, got {value!r}"
        ) from None
    return raw


def _ts(row: dict[str, Any], key: str) -> Optional[str]:
    v = row.get(key)
    return v if isinstance(v, str) and v else None


def in_window(row: dict[str, Any], key: str, since: Optional[str]) -> bool:
    """True if row[key] is on/after `since`. Rows with no timestamp are KEPT.

    Keeping undated rows is deliberate: dropping them would silently shrink
    the sample and make the totals look cleaner than the data is.
    """
    if since is None:
        return True
    ts = _ts(row, key)
    if ts is None:
        return True
    return ts[:10] >= since


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def summarize_trades(
    trades: list[dict[str, Any]], since: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Group trades by bot_id, split live vs paper. Never combines them.

    A trade counts as CLOSED when pnl_usd is present — entries carry no
    P/L, exits do. Win rate and gross P/L are computed over closed trades
    only.
    """
    out: dict[str, dict[str, Any]] = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        bot = t.get("bot_id")
        if not bot:
            continue
        if not in_window(t, "occurred_at", since):
            continue
        bucket = out.setdefault(bot, {
            "live":  {"closed": 0, "wins": 0, "gross_pnl": 0.0},
            "paper": {"closed": 0, "wins": 0, "gross_pnl": 0.0},
            "total_rows": 0,
        })
        bucket["total_rows"] += 1
        leg = bucket["paper"] if t.get("is_paper") else bucket["live"]
        pnl = _num(t.get("pnl_usd"))
        if pnl is None:
            continue           # an entry, or an exit with no P/L recorded
        leg["closed"] += 1
        leg["gross_pnl"] += pnl
        if pnl > 0:
            leg["wins"] += 1
    return out


def win_rate(wins: int, closed: int) -> Optional[float]:
    """Decimal win rate, or None when there is nothing to divide.

    None means "unknown". It must never render as 0% — that would read as
    "lost every trade" when it means "no trades".
    """
    if not closed:
        return None
    return wins / float(closed)


def summarize_runs(
    runs: list[dict[str, Any]], since: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Per-bot run counts, first/last timestamps and reported error totals."""
    out: dict[str, dict[str, Any]] = {}
    for r in runs or []:
        if not isinstance(r, dict):
            continue
        bot = r.get("bot_id")
        if not bot or not in_window(r, "started_at", since):
            continue
        b = out.setdefault(bot, {
            "runs": 0, "first": None, "last": None,
            "reported_errors": 0, "reported_trades": 0,
            "last_status": None, "still_running": 0,
        })
        b["runs"] += 1
        started = _ts(r, "started_at")
        if started:
            if b["first"] is None or started < b["first"]:
                b["first"] = started
            if b["last"] is None or started > b["last"]:
                b["last"] = started
                b["last_status"] = r.get("status")
        b["reported_errors"] += int(_num(r.get("error_count")) or 0)
        b["reported_trades"] += int(_num(r.get("trade_count")) or 0)
        if r.get("status") == "running":
            b["still_running"] += 1
    return out


def count_by_bot(rows: list[dict[str, Any]], ts_key: str,
                 since: Optional[str] = None,
                 where: Optional[dict[str, Any]] = None) -> dict[str, int]:
    """Generic per-bot counter with an optional equality filter."""
    out: dict[str, int] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        bot = r.get("bot_id")
        if not bot or not in_window(r, ts_key, since):
            continue
        if where and any(r.get(k) != v for k, v in where.items()):
            continue
        out[bot] = out.get(bot, 0) + 1
    return out


def expenses_by_bot(
    expenses: list[dict[str, Any]], since: Optional[str] = None,
) -> tuple[dict[str, float], float]:
    """(per-bot expenses, unattributed league-wide expenses).

    bot_expenses.bot_id is nullable by design — Fly hosting for the shared
    agent_runner is not attributable to one bot. Unattributed cost is
    returned separately rather than smeared across bots, because any
    allocation rule would be invented.

    `period` is 'YYYY-MM' or 'YYYY'. We sum amount_usd for periods inside
    the window and IGNORE the `recurring` flag: the seed writes one row per
    month per standing cost, so summing rows is correct and honouring
    `recurring` on top of that double-counts (the dashboard bug found in
    the 2026-08 audit).
    """
    per_bot: dict[str, float] = {}
    league_wide = 0.0
    since_month = since[:7] if since else None
    since_year = since[:4] if since else None

    for e in expenses or []:
        if not isinstance(e, dict):
            continue
        amt = _num(e.get("amount_usd"))
        if amt is None:
            continue
        period = str(e.get("period") or "").strip()
        if since_month and period:
            if len(period) == 7 and period < since_month:
                continue
            if len(period) == 4 and period < since_year:  # type: ignore[operator]
                continue
        bot = e.get("bot_id")
        if bot:
            per_bot[bot] = per_bot.get(bot, 0.0) + amt
        else:
            league_wide += amt
    return per_bot, league_wide


def build_notes(
    bot_id: str,
    *,
    live_closed: int,
    paper_closed: int,
    open_positions: int,
    reported_errors: int,
    actual_error_rows: Optional[int],
    still_running: int,
    all_time: bool,
) -> list[str]:
    """Every caveat that applies to this bot's row. Order: loudest first."""
    notes: list[str] = []

    if live_closed == 0 and paper_closed == 0:
        notes.append("NO CLOSED TRADES — nothing to evaluate")
    else:
        if 0 < live_closed < MIN_MEANINGFUL_N:
            notes.append(f"n<{MIN_MEANINGFUL_N} live (n={live_closed}) — "
                         f"not statistically meaningful")
        if 0 < paper_closed < MIN_MEANINGFUL_N:
            notes.append(f"n<{MIN_MEANINGFUL_N} paper (n={paper_closed})")

    if live_closed or paper_closed:
        notes.append("P/L is GROSS — fees_usd is 0/missing on every row")

    if actual_error_rows is not None and reported_errors > actual_error_rows:
        notes.append(
            f"reported errors ({reported_errors}) EXCEED actual bot_errors "
            f"rows ({actual_error_rows}) — monitor counter inflation")

    if still_running:
        notes.append(f"{still_running} run(s) still status='running' — "
                     f"orphaned rows inflate run counts")

    if open_positions:
        notes.append(f"{open_positions} open position(s) NOT valued in v1")

    if all_time:
        notes.append("ALL-TIME window spans the pre-2026-08-09 system "
                     "(stop-losses were disabled several ways)")

    notes.extend(KNOWN_GAPS.get(bot_id, ()))
    return notes


def fmt_money(v: Optional[float]) -> str:
    return "     —" if v is None else f"{v:+9.2f}"


def fmt_pct(v: Optional[float]) -> str:
    return "    —" if v is None else f"{v * 100:6.1f}%"


def fmt_n(v: Optional[int]) -> str:
    return "  —" if v is None else f"{v:3d}"


def fmt_date(ts: Optional[str]) -> str:
    return "—         " if not ts else ts[:10]


# ═══════════════════════════════════════════════════════════════════════════
#  DETAIL MODE (--bot) — one bot, up close.
# ═══════════════════════════════════════════════════════════════════════════
#
# The overview answers "which bot should I look at?". This answers "what is
# that bot actually doing?" — the question you otherwise answer by opening
# the Supabase table editor and writing the same five queries by hand.
#
# Same rules as the overview: GET only, League project only, server-side
# filters, URL-encoded, paginated. The bot_id filter is pushed into the
# query rather than applied after, so a chatty bot cannot push a quiet
# one's rows past the page limit.

DETAIL_ROWS = 20          # per section


#: Bots that do NOT mirror position state into League bot_positions.
#:
#: For these, an empty League positions result says NOTHING about what the
#: bot holds. Printing "(no open positions)" would be a false statement
#: about the portfolio rather than about the query — the same error class
#: as reporting 0 runs when the fetch was truncated.
#:
#: Verified 2026-09-22 by grepping for league.upsert_position /
#: league.close_position across bots/:
#:
#:   etf_rotation_v1     mirrors  (main.py:142,242,318,379)
#:   short_watchlist_v1  mirrors  (main.py:171,231)
#:   stock_momentum_v1   DOES NOT — its close_position (bot.py:554) writes
#:                       to the STOCK project's own `positions` table
#:   crypto_ema_atr_v1   DOES NOT — positions live in trader.positions and
#:                       the crypto project's own bot_state
#:
#: Research bots (bond, agent) open no positions at all, so an empty result
#: is genuinely empty for them and needs no warning.
POSITIONS_NOT_MIRRORED: dict[str, str] = {
    "stock_momentum_v1":
        "Stock position state is written to the stock project's own "
        "`positions` table. League mirroring was deferred (see migration "
        "005 notes), so bot_positions is NOT authoritative for this bot.",
    "crypto_ema_atr_v1":
        "Crypto position state lives in trader.positions and the crypto "
        "project's own bot_state. It is not mirrored into League "
        "bot_positions, so this table is NOT authoritative for this bot.",
}

DETAIL_SPECS: dict[str, dict[str, Any]] = {
    "runs": {
        "table": "bot_runs",
        "select": ("id,started_at,ended_at,status,trade_count,error_count,"
                   "duration_ms,trigger,notes"),
        "ts": "started_at", "order": "started_at.desc",
        "since_key": "started_at",
    },
    "trades": {
        "table": "bot_trades",
        "select": ("occurred_at,symbol,side,price,quantity,amount_usd,"
                   "pnl_usd,pnl_pct,is_paper,strategy,reason,order_id"),
        "ts": "occurred_at", "order": "occurred_at.desc",
        "since_key": "occurred_at",
    },
    "positions": {
        "table": "bot_positions",
        "select": ("symbol,status,quantity,entry_price,entry_at,amount_usd,"
                   "pnl_usd,is_paper,metadata,updated_at"),
        "ts": "updated_at", "order": "updated_at.desc",
        # Deliberately unwindowed: an open position may predate --since and
        # still be open. ETF has held four since May.
        "since_key": None,
        # PostgREST filters before LIMIT; closed rows must not crowd out opens.
        "filters": {"status": "eq.open"},
    },
    "errors": {
        "table": "bot_errors",
        "select": "occurred_at,stage,symbol,severity,error_type,message",
        "ts": "occurred_at", "order": "occurred_at.desc",
        "since_key": "occurred_at",
    },
    "events": {
        "table": "bot_events",
        "select": "occurred_at,event_type,symbol,message",
        "ts": "occurred_at", "order": "occurred_at.desc",
        "since_key": "occurred_at",
    },
}


def build_detail_query(
    spec: dict[str, Any], bot_id: str, since: Optional[str] = None,
    *, limit: int = DETAIL_ROWS,
) -> str:
    """One bot's slice of a table. Pure. URL-encoded, filtered server-side.

    No pagination: every section is capped at `limit` rows by design — this
    is a "latest N" view, not an aggregate. Because the cap is intentional
    rather than accidental, a full page here means "there are more, as
    expected", not "data may be missing". That is the opposite of the
    overview, where a full page was a silent truncation bug.
    """
    params: list[tuple[str, str]] = [
        ("select", spec["select"]),
        ("bot_id", f"eq.{bot_id}"),
    ]
    params.extend(spec.get("filters", {}).items())
    since_key = spec.get("since_key")
    if since_key and since:
        params.append((since_key, f"gte.{since}"))
    if spec.get("order"):
        params.append(("order", spec["order"]))
    params.append(("limit", str(int(limit))))
    return f"{spec['table']}?{urlencode(params)}"


def _cell(v: Any, width: int, *, num: bool = False, places: int = 2) -> str:
    """Render one table cell. None becomes '—', never 0 or ''."""
    if v is None or v == "":
        return f"{'—':>{width}}" if num else f"{'—':<{width}}"
    if num:
        f = _num(v)
        return f"{'—':>{width}}" if f is None else f"{f:>{width}.{places}f}"
    s = str(v)
    return f"{s[:width]:<{width}}"


def _section(title: str, rows: Optional[list[dict]], render, *,
             empty_text: str = "(none in this window)") -> list[str]:
    """One detail section, with an honest empty/unreadable distinction.

    `None` means the fetch FAILED. Printing "none" for that would be a
    false statement about the bot rather than about the query — the same
    class of error as reporting 0 runs when the query was truncated.
    """
    out = ["", title, "-" * 100]
    if rows is None:
        out.append("  ⚠ UNREADABLE — this table could not be fetched. "
                   "Its absence below is NOT evidence of no activity.")
        return out
    if not rows:
        out.append(f"  {empty_text}")
        return out
    out.extend(render(rows))
    return out


def render_bot_detail(
    bot_id: str,
    reg: Optional[dict[str, Any]],
    status: Optional[dict[str, Any]],
    sections: dict[str, Optional[list[dict]]],
    since: Optional[str],
) -> str:
    """Assemble the --bot report. Pure given its inputs — no IO."""
    L: list[str] = []
    window = "ALL TIME" if since is None else f"since {since}"
    L.append("=" * 100)
    L.append(f"  BOT DETAIL — {bot_id}   ({window})")
    L.append(f"  read-only, League project only, no market data")
    L.append("=" * 100)

    # ── 1. Header ─────────────────────────────────────────────────────────
    if reg is None and status is None:
        L.append("")
        L.append(f"  ⚠ {bot_id} not found in bot_registry or bot_status.")
        L.append("    Either the id is wrong, or the bot has never registered.")
    r, s = reg or {}, status or {}
    L.append("")
    L.append(f"  type          {r.get('bot_type') or '—'}")
    L.append(f"  mode          {r.get('mode') or '—'}")
    L.append(f"  registry      {r.get('status') or '—'}"
             f"   can_place_orders={r.get('can_place_orders')}"
             f"   max_order_usd={r.get('max_order_usd')}")
    L.append(f"  health        {s.get('health') or '—'}")
    L.append(f"  heartbeat     {(s.get('last_heartbeat_at') or '—')[:19]}")
    L.append(f"  last run      {s.get('last_run_status') or '—'}"
             f"   id={s.get('last_run_id') or '—'}")
    if s.get("last_error_msg"):
        L.append(f"  last error    {str(s.get('last_error_msg'))[:80]}")

    # ── 2. Runs ───────────────────────────────────────────────────────────
    def _runs(rows):
        o = [f"  {'started':<20}{'ended':<20}{'status':<9}"
             f"{'trades':>7}{'errors':>7}{'dur(s)':>9}  notes"]
        for x in rows[:DETAIL_ROWS]:
            dur = _num(x.get("duration_ms"))
            note = ""
            st = (x.get("status") or "").lower()
            if st == "running":
                note = "ORPHANED? still 'running'"
            elif st in ("failed", "timeout"):
                note = "FAILED"
            elif st == "warning":
                note = "degraded"
            if x.get("notes"):
                note = f"{note} | {str(x['notes'])[:40]}".strip(" |")
            o.append(
                f"  {_cell(str(x.get('started_at') or '')[:19], 20)}"
                f"{_cell(str(x.get('ended_at') or '')[:19], 20)}"
                f"{_cell(x.get('status'), 9)}"
                f"{_cell(x.get('trade_count'), 7, num=True, places=0)}"
                f"{_cell(x.get('error_count'), 7, num=True, places=0)}"
                f"{_cell(dur / 1000.0 if dur else None, 9, num=True, places=1)}"
                f"  {note}")
        return o

    L.extend(_section(f"RECENT RUNS (latest {DETAIL_ROWS})",
                      sections.get("runs"), _runs))

    # ── 3. Trades ─────────────────────────────────────────────────────────
    def _trades(rows):
        o = [f"  {'occurred':<20}{'sym':<8}{'side':<6}{'price':>10}"
             f"{'qty':>12}{'amt$':>9}{'pnl$':>9}{'pnl%':>8}  {'L/P':<4}strategy"]
        for x in rows[:DETAIL_ROWS]:
            pp = _num(x.get("pnl_pct"))
            o.append(
                f"  {_cell(str(x.get('occurred_at') or '')[:19], 20)}"
                f"{_cell(x.get('symbol'), 8)}"
                f"{_cell(x.get('side'), 6)}"
                f"{_cell(x.get('price'), 10, num=True)}"
                f"{_cell(x.get('quantity'), 12, num=True, places=8)}"
                f"{_cell(x.get('amount_usd'), 9, num=True)}"
                f"{_cell(x.get('pnl_usd'), 9, num=True)}"
                f"{(f'{pp * 100:7.2f}%' if pp is not None else '       —')}"
                f"  {'PAPER' if x.get('is_paper') else 'LIVE ':<4}"
                f"{str(x.get('strategy') or '—')[:22]}")
        # Live and paper are listed together here but never AGGREGATED —
        # the L/P column is the whole point. Any summing belongs in the
        # overview, which keeps them in separate columns.
        o.append("")
        o.append("  L/P column separates live from paper. This section does "
                 "NOT total them.")
        return o

    L.extend(_section(f"RECENT TRADES (latest {DETAIL_ROWS})",
                      sections.get("trades"), _trades))

    # ── 4. Open positions ─────────────────────────────────────────────────
    def _positions(rows):
        open_rows = [x for x in rows if (x.get("status") or "") == "open"]
        gap = POSITIONS_NOT_MIRRORED.get(bot_id)
        warning = ([f"  ⚠ HOLDINGS UNVERIFIED — {gap}"] if gap else [])
        if not open_rows:
            return warning or ["  (no open positions)"]
        o = warning + [f"  {'symbol':<8}{'entry_at':<20}{'entry':>10}{'qty':>12}"
             f"{'amt$':>9}{'mark_pnl$':>11}  {'L/P':<6}age"]
        for x in open_rows:
            meta = x.get("metadata") or {}
            mark = meta.get("mark_pnl_usd") if isinstance(meta, dict) else None
            entry_at = str(x.get("entry_at") or "")[:19]
            age = ""
            try:
                if entry_at:
                    d = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(entry_at.replace("Z", "+00:00"))
                         .replace(tzinfo=timezone.utc)).days
                    age = f"{d}d"
            except Exception:  # noqa: BLE001
                age = ""
            o.append(
                f"  {_cell(x.get('symbol'), 8)}"
                f"{_cell(entry_at, 20)}"
                f"{_cell(x.get('entry_price'), 10, num=True)}"
                f"{_cell(x.get('quantity'), 12, num=True, places=8)}"
                f"{_cell(x.get('amount_usd'), 9, num=True)}"
                f"{_cell(mark, 11, num=True)}"
                f"  {'PAPER' if x.get('is_paper') else 'LIVE ':<6}{age}")
        o.append("")
        o.append("  ⚠ mark_pnl$ is UNREALIZED and UNAUDITED. It is read from")
        o.append("    bot_positions.metadata.mark_pnl_usd, written by the bot's")
        o.append("    own mark-to-market — not from a broker statement, and not")
        o.append("    from the pnl_usd column (which means REALIZED elsewhere).")
        o.append("    A blank means the position has never been marked at all.")
        return o

    # An empty result means "no open positions" ONLY for bots that actually
    # mirror position state into League. For the others it means "this
    # table does not know", which is a different statement and must not be
    # rendered as a portfolio fact.
    _pos_gap = POSITIONS_NOT_MIRRORED.get(bot_id)
    L.extend(_section(
        "OPEN POSITIONS", sections.get("positions"), _positions,
        empty_text=(f"⚠ HOLDINGS UNVERIFIED — {_pos_gap}" if _pos_gap
                    else "(no open positions)"),
    ))

    # ── 5. Errors ─────────────────────────────────────────────────────────
    def _errors(rows):
        o = [f"  {'occurred':<20}{'stage':<14}{'sym':<7}{'sev':<9}message"]
        for x in rows[:DETAIL_ROWS]:
            o.append(
                f"  {_cell(str(x.get('occurred_at') or '')[:19], 20)}"
                f"{_cell(x.get('stage'), 14)}"
                f"{_cell(x.get('symbol'), 7)}"
                f"{_cell(x.get('severity'), 9)}"
                f"{str(x.get('message') or '')[:44]}")
        return o

    L.extend(_section(f"RECENT ERRORS (latest {DETAIL_ROWS})",
                      sections.get("errors"), _errors))

    # ── 6. Events ─────────────────────────────────────────────────────────
    def _events(rows):
        o = [f"  {'occurred':<20}{'event':<22}{'sym':<7}message"]
        for x in rows[:DETAIL_ROWS]:
            o.append(
                f"  {_cell(str(x.get('occurred_at') or '')[:19], 20)}"
                f"{_cell(x.get('event_type'), 22)}"
                f"{_cell(x.get('symbol'), 7)}"
                f"{str(x.get('message') or '')[:44]}")
        return o

    L.extend(_section(f"RECENT EVENTS (latest {DETAIL_ROWS})",
                      sections.get("events"), _events))

    L.append("")
    L.append("=" * 100)
    L.append("  Each section shows the latest rows only — a full section means")
    L.append("  'more exist', not 'data missing'. For totals use the overview:")
    L.append("    python -m scripts.league_scoreboard")
    L.append("=" * 100)
    return "\n".join(L)


def fetch_bot_detail(
    cfg: dict[str, str], bot_id: str, since: Optional[str],
    *, getter: Any = None,
) -> dict[str, Optional[list[dict]]]:
    """Fetch every detail section for one bot. GET only."""
    fetch = getter or _get
    out: dict[str, Optional[list[dict]]] = {}
    for key, spec in DETAIL_SPECS.items():
        out[key] = fetch(cfg, build_detail_query(spec, bot_id, since))
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  READ LAYER — GET only.
# ═══════════════════════════════════════════════════════════════════════════


def _config() -> Optional[dict[str, str]]:
    """League Supabase config, read at call time. Never printed."""
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    if not url or not key:
        return None
    return {"url": url, "key": key}


def _get(cfg: dict[str, str], path: str, timeout: float = 20.0) -> Optional[list[dict]]:
    """GET one PostgREST path. None on failure — distinct from empty."""
    if requests is None:
        return None
    try:
        resp = requests.get(
            f"{cfg['url']}/rest/v1/{path}",
            headers={
                "apikey": cfg["key"],
                "Authorization": f"Bearer {cfg['key']}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[scoreboard] GET {path.split('?')[0]} failed: {e!r}")
        return None
    if resp.status_code >= 400:
        print(f"[scoreboard] GET {path.split('?')[0]}: HTTP {resp.status_code}")
        return None
    try:
        rows = resp.json()
    except Exception:  # noqa: BLE001
        print(f"[scoreboard] GET {path.split('?')[0]}: non-JSON response")
        return None
    return rows if isinstance(rows, list) else None


def fetch_all(
    cfg: dict[str, str], since: Optional[str] = None,
) -> tuple[dict[str, Optional[list[dict]]], dict[str, dict[str, Any]]]:
    """Fetch every table. Returns (rows_by_key, freshness_by_key).

    Queries are built from TABLE_SPECS, newest-first, with the window
    pushed server-side where it applies. A missing or unreadable table
    degrades to None and is reported as such rather than as "empty" —
    those are different facts and only one of them means "no activity".
    """
    data: dict[str, Optional[list[dict]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for key, spec in TABLE_SPECS.items():
        rows, page_meta = fetch_paginated(cfg, spec, since)
        data[key] = rows
        meta[key] = table_freshness(rows, spec.get("ts"), page_meta)
        meta[key]["table"] = spec["table"]
    return data, meta


# ═══════════════════════════════════════════════════════════════════════════
#  RENDER
# ═══════════════════════════════════════════════════════════════════════════


def render_freshness(meta: dict[str, dict[str, Any]]) -> list[str]:
    """The DATA FRESHNESS block.

    This exists because of a real incident: the scoreboard reported 0 runs
    since 2026-08-09 while the League project held 9,726 runs with the
    newest at 2026-09-22. The query had fetched the oldest 5,000. Nothing
    in the output hinted at it — the table simply looked empty, which is
    indistinguishable from "the bots did not run".

    A TRUNCATED marker makes that self-diagnosing. Same principle as the
    NOTES section: how much to trust a number must be as visible as the
    number.
    """
    if not meta:
        return []
    out = ["", "DATA FRESHNESS", "-" * 100]
    for key in TABLE_SPECS:
        m = meta.get(key)
        if not m:
            continue
        name = m.get("table", key)
        if m.get("unreadable"):
            out.append(f"  {name:<18}{'UNREADABLE':>10}   "
                       f"could not be fetched — figures above EXCLUDE this table")
            continue
        n = m.get("rows") or 0
        latest = m.get("latest")
        pages = m.get("pages", 1)
        line = (f"  {name:<18}{n:>6} rows  {pages:>3}pg   "
                f"latest {(latest[:19] + 'Z') if latest else '—'}")
        if m.get("partial"):
            line += "   ⚠ PARTIAL — a page failed mid-scan"
        elif m.get("truncated"):
            line += "   ⚠ TRUNCATED — safety guard stopped paging"
        out.append(line)
    if any(m.get("truncated") or m.get("partial") for m in meta.values()):
        out.append("")
        out.append(f"  Paging stops when a page returns fewer than {PAGE_SIZE} "
                   f"rows. TRUNCATED means the")
        out.append(f"  {MAX_PAGES}-page / {MAX_ROWS}-row guard stopped us "
                   f"first; PARTIAL means a page failed.")
        out.append("  Rows are NEWEST-FIRST, so anything missing is the "
                   "OLDEST history. Narrow --since.")
    return out


def build_report(data: dict[str, Optional[list[dict]]], since: Optional[str],
                 meta: Optional[dict[str, dict[str, Any]]] = None) -> str:
    """Assemble the whole report. Pure given `data` — no IO."""
    all_time = since is None
    registry = data.get("registry") or []
    status_by = {s.get("bot_id"): s for s in (data.get("status") or [])
                 if isinstance(s, dict)}

    runs = summarize_runs(data.get("runs") or [], since)
    trades = summarize_trades(data.get("trades") or [], since)
    # Positions describe current state, independently of the history window.
    open_pos = count_by_bot(data.get("positions") or [], "updated_at", None,
                            where={"status": "open"})
    err_rows = data.get("errors")
    errs = (count_by_bot(err_rows, "occurred_at", since)
            if err_rows is not None else None)
    exp_by_bot, exp_league = expenses_by_bot(data.get("expenses") or [], since)

    bots = sorted({b.get("bot_id") for b in registry if isinstance(b, dict) and b.get("bot_id")}
                  | set(runs) | set(trades))

    L: list[str] = []
    window = "ALL TIME" if all_time else f"since {since}"
    L.append("=" * 100)
    L.append(f"  LEAGUE SCOREBOARD — {window}")
    L.append(f"  generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z"
             f"   |   read-only, League project only, no market data")
    L.append("=" * 100)
    if all_time:
        L.append("  ⚠ ALL-TIME: includes the pre-2026-08-09 system, when stop-losses")
        L.append("    were disabled in five distinct ways. Trends across that line are")
        L.append("    comparing two different programs.")
        L.append("")

    # ── Activity ──────────────────────────────────────────────────────────
    L.append("")
    L.append("ACTIVITY")
    L.append("-" * 100)
    L.append(f"{'bot_id':<22}{'type':<17}{'mode':<10}{'runs':>6}"
             f"  {'first':<11}{'last':<11}{'status':<10}{'err(rep)':>9}{'err(rows)':>10}")
    L.append("-" * 100)
    for bot in bots:
        reg = next((r for r in registry if r.get("bot_id") == bot), {})
        r = runs.get(bot, {})
        st = status_by.get(bot, {})
        L.append(
            f"{bot:<22}{str(reg.get('bot_type') or '—'):<17}"
            f"{str(reg.get('mode') or '—'):<10}{r.get('runs', 0):>6}"
            f"  {fmt_date(r.get('first')):<11}{fmt_date(r.get('last')):<11}"
            f"{str(r.get('last_status') or st.get('last_run_status') or '—'):<10}"
            f"{r.get('reported_errors', 0):>9}"
            f"{(fmt_n(errs.get(bot, 0)) if errs is not None else '  n/a'):>10}"
        )

    # ── Performance ───────────────────────────────────────────────────────
    L.append("")
    L.append("REALIZED P/L — GROSS OF FEES.  LIVE AND PAPER ARE NEVER COMBINED.")
    L.append("-" * 100)
    L.append(f"{'bot_id':<22}| {'LIVE n':>7}{'win':>7}{'gross$':>11}"
             f" | {'PAPER n':>8}{'win':>7}{'gross$':>11} |{'open':>6}{'exp$':>9}{'net$':>10}")
    L.append("-" * 100)
    for bot in bots:
        t = trades.get(bot, {})
        live = t.get("live", {"closed": 0, "wins": 0, "gross_pnl": 0.0})
        paper = t.get("paper", {"closed": 0, "wins": 0, "gross_pnl": 0.0})
        exp = exp_by_bot.get(bot, 0.0)
        live_gross = live["gross_pnl"] if live["closed"] else None
        net = (live_gross - exp) if (live_gross is not None and exp) else None
        L.append(
            f"{bot:<22}| {fmt_n(live['closed']):>7}"
            f"{fmt_pct(win_rate(live['wins'], live['closed'])):>7}"
            f"{fmt_money(live_gross):>11}"
            f" | {fmt_n(paper['closed']):>8}"
            f"{fmt_pct(win_rate(paper['wins'], paper['closed'])):>7}"
            f"{fmt_money(paper['gross_pnl'] if paper['closed'] else None):>11}"
            # 'N/A' rather than 0 for bots that do not mirror positions into
            # League — 0 would assert a flat portfolio this table cannot see.
            f" |{('N/A' if bot in POSITIONS_NOT_MIRRORED else open_pos.get(bot, 0)):>6}"
            f"{(f'{exp:8.2f}' if exp else '       —'):>9}"
            f"{fmt_money(net):>10}"
        )
    L.append("-" * 100)
    if exp_league:
        L.append(f"{'(league-wide, unattributed)':<22}"
                 f"{'':<48}{exp_league:>26.2f}")
        L.append("  Unattributed cost is NOT allocated across bots — any split "
                 "would be invented.")

    # ── Notes ─────────────────────────────────────────────────────────────
    L.append("")
    L.append("NOTES — read these before trusting any number above")
    L.append("-" * 100)
    any_notes = False
    for bot in bots:
        t = trades.get(bot, {})
        live = t.get("live", {"closed": 0})
        paper = t.get("paper", {"closed": 0})
        r = runs.get(bot, {})
        notes = build_notes(
            bot,
            live_closed=live["closed"],
            paper_closed=paper["closed"],
            # Suppress the "N open position(s) NOT valued" note for bots
            # whose position rows League never receives — that note would
            # imply the count is meaningful.
            open_positions=(0 if bot in POSITIONS_NOT_MIRRORED
                            else open_pos.get(bot, 0)),
            reported_errors=r.get("reported_errors", 0),
            actual_error_rows=(errs.get(bot, 0) if errs is not None else None),
            still_running=r.get("still_running", 0),
            all_time=all_time,
        )
        if not notes:
            continue
        any_notes = True
        L.append(f"\n  {bot}")
        for n in notes:
            L.append(f"    • {n}")
    if not any_notes:
        L.append("  (none)")

    L.extend(render_freshness(meta or {}))

    L.append("")
    L.append("=" * 100)
    L.append("  v1 OMITS: unrealized P/L, SPY/QQQ benchmarks, time-weighted return,")
    L.append("  drawdown. The last three need an equity curve; nothing records")
    L.append("  account equity over time. Per-trade P/L cannot substitute for it.")
    L.append("=" * 100)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only League scoreboard (no orders, no market data).")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help=f"window start (default {DEFAULT_SINCE})")
    ap.add_argument("--all", action="store_true",
                    help="all-time; includes the pre-2026-08-09 system")
    ap.add_argument("--bot", metavar="BOT_ID",
                    help="focused detail report for one bot instead of the "
                         "overview (e.g. --bot stock_momentum_v1)")
    args = ap.parse_args(argv)

    try:
        since = parse_since(args.since, all_time=args.all)
    except ValueError as e:
        print(f"[scoreboard] {e}")
        return 2

    cfg = _config()
    if cfg is None:
        print("[scoreboard] LEAGUE_SUPABASE_URL / LEAGUE_SUPABASE_KEY not set.")
        print("[scoreboard] This script is read-only and needs only those two.")
        return 2

    # ── Detail mode ───────────────────────────────────────────────────────
    if args.bot:
        bot_id = args.bot.strip()
        if not bot_id:
            print("[scoreboard] --bot requires a bot_id")
            return 2
        reg_rows = _get(cfg, build_detail_query(
            {"table": "bot_registry", "select": "*", "order": None,
             "since_key": None}, bot_id, None, limit=1))
        st_rows = _get(cfg, build_detail_query(
            {"table": "bot_status", "select": "*", "order": None,
             "since_key": None}, bot_id, None, limit=1))
        sections = fetch_bot_detail(cfg, bot_id, since)
        if all(v is None for v in sections.values()) and reg_rows is None:
            print(f"[scoreboard] could not read any table for {bot_id} — "
                  f"reporting nothing rather than an empty detail page that "
                  f"looks like 'this bot does nothing'.")
            return 1
        print(render_bot_detail(
            bot_id,
            (reg_rows or [None])[0] if reg_rows else None,
            (st_rows or [None])[0] if st_rows else None,
            sections, since,
        ))
        return 0

    # ── Overview (unchanged) ──────────────────────────────────────────────
    data, meta = fetch_all(cfg, since)
    if data.get("registry") is None and data.get("runs") is None:
        print("[scoreboard] could not read bot_registry or bot_runs — "
              "reporting nothing rather than an empty scoreboard that looks "
              "like 'no activity'.")
        return 1

    print(build_report(data, since, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
