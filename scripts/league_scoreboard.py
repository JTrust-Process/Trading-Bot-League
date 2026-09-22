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

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


#: Everything before this is a different system. See the module docstring.
DEFAULT_SINCE = "2026-08-09"

#: Below this, an average is an anecdote.
MIN_MEANINGFUL_N = 30

#: Per-bot data-quality gaps, from the 2026-09 audit. Surfaced in NOTES so
#: nobody reads a column that is structurally empty as a real zero.
KNOWN_GAPS: dict[str, tuple[str, ...]] = {
    "stock_momentum_v1": (
        "bot_trades rows carry no quantity/order_id/run_id "
        "(league_status.log_trade call omits them) -> cannot reconcile "
        "against Public, and the (bot_id, order_id) dedup index is inert",
        "run error_count before the 2026-09-22 monitor fix is INFLATED: the "
        "singleton accumulated across runs in one long-lived process",
    ),
    "crypto_ema_atr_v1": (
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
#  PURE HELPERS — no IO, no network. Unit-tested.
# ═══════════════════════════════════════════════════════════════════════════


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


def fetch_all(cfg: dict[str, str]) -> dict[str, Optional[list[dict]]]:
    """Every table this report reads. A missing table degrades to None."""
    return {
        "registry":  _get(cfg, "bot_registry?select=*"),
        "status":    _get(cfg, "bot_status?select=*"),
        "runs":      _get(cfg, "bot_runs?select=bot_id,started_at,status,"
                               "trade_count,error_count&order=started_at.asc&limit=5000"),
        "trades":    _get(cfg, "bot_trades?select=bot_id,occurred_at,side,pnl_usd,"
                               "is_paper,strategy,symbol&order=occurred_at.asc&limit=5000"),
        "positions": _get(cfg, "bot_positions?select=bot_id,status,symbol,updated_at"),
        "errors":    _get(cfg, "bot_errors?select=bot_id,occurred_at&limit=5000"),
        "expenses":  _get(cfg, "bot_expenses?select=bot_id,amount_usd,period,"
                               "category,recurring"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  RENDER
# ═══════════════════════════════════════════════════════════════════════════


def build_report(data: dict[str, Optional[list[dict]]], since: Optional[str]) -> str:
    """Assemble the whole report. Pure given `data` — no IO."""
    all_time = since is None
    registry = data.get("registry") or []
    status_by = {s.get("bot_id"): s for s in (data.get("status") or [])
                 if isinstance(s, dict)}

    runs = summarize_runs(data.get("runs") or [], since)
    trades = summarize_trades(data.get("trades") or [], since)
    open_pos = count_by_bot(data.get("positions") or [], "updated_at", since,
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
    L.append(f"{'bot_id':<22}{'type':<10}{'mode':<8}{'runs':>6}"
             f"  {'first':<11}{'last':<11}{'status':<10}{'err(rep)':>9}{'err(rows)':>10}")
    L.append("-" * 100)
    for bot in bots:
        reg = next((r for r in registry if r.get("bot_id") == bot), {})
        r = runs.get(bot, {})
        st = status_by.get(bot, {})
        L.append(
            f"{bot:<22}{str(reg.get('bot_type') or '—'):<10}"
            f"{str(reg.get('mode') or '—'):<8}{r.get('runs', 0):>6}"
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
            f" |{open_pos.get(bot, 0):>6}"
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
            open_positions=open_pos.get(bot, 0),
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

    data = fetch_all(cfg)
    if data.get("registry") is None and data.get("runs") is None:
        print("[scoreboard] could not read bot_registry or bot_runs — "
              "reporting nothing rather than an empty scoreboard that looks "
              "like 'no activity'.")
        return 1

    print(build_report(data, since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
