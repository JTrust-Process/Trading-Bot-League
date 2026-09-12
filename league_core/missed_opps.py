"""league_core.missed_opps — record trades the bot DIDN'T take, and score them.

PHASE 1 OF THE AI LAYER. CONTAINS NO AI.

No LLM, no API key, no paid dependency, no new package. This module writes
rows describing candidates a bot rejected, and later fills in what those
symbols actually did. That is the whole scope.

WHY IT EXISTS
    The bot's trade log records what it did. It has never recorded what it
    declined to do, so "the entry filters are too strict" and "the entry
    filters are correctly protective" have been equally unfalsifiable. This
    module makes the counterfactual observable. It is deliberately built
    BEFORE any scoring layer, because a scorer with no ground truth is just
    a confident guess.

SAFETY MODEL — read this before extending anything here

    1. OFF BY DEFAULT. `tracking_enabled()` returns False unless
       MISSED_OPP_TRACKING is explicitly truthy. Every public function
       checks it FIRST, before touching config, network, or env. Unset
       means not a single byte leaves the process.

    2. NEVER RAISES. Every public function wraps its body in
       `except BaseException`. The recording path runs inside a live
       trading loop; an exception escaping this module could abort a cycle
       and leave positions unmanaged. There is no failure mode here worth
       more than a skipped analytics row.

    3. NO TRADING IMPORTS. This module must never import an order client,
       a sizing helper, or risk.py. It has no access to credentials that
       could place an order. Keep it that way — the safety property is
       "cannot", not "does not".

    4. WRITES ONE TABLE. bot_missed_opportunities, and nothing else. Never
       trades, positions, bot_state, bot_approvals, or bot_registry.

    5. LAZY ENV READS. Same rule as league_core.status: the bots call
       load_dotenv() inside main(), so any module-level os.getenv would
       capture stale values.

REUSED PATTERNS
    Config and PostgREST access mirror league_core.status (_config/_post/
    _patch) rather than introducing a second convention. Bars come from
    league_core.public_bars — Public.com is this project's bar source.
    yfinance is deliberately not used.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - every bot already has requests
    requests = None  # type: ignore[assignment]


TABLE = "bot_missed_opportunities"

# Trading days to wait before a horizon is considered measurable. Calendar
# days are ~1.45x trading days; we use a generous multiplier plus slack so a
# holiday week can't make us score a horizon with too few bars.
_HORIZON_MIN_CALENDAR_DAYS = {1: 2, 5: 9, 20: 32}


# ── Env ──────────────────────────────────────────────────────────────────────


def tracking_enabled() -> bool:
    """True only when MISSED_OPP_TRACKING is explicitly switched on.

    Accepted truthy values: "1", "true", "yes", "on" (case-insensitive).
    ANYTHING else — including unset, "", "0", "false", or a typo — is off.

    This is the master switch. It is checked before config, before network,
    before env beyond this one variable. An operator who has not opted in
    gets byte-for-byte the behaviour they had before this module existed.
    """
    try:
        return os.getenv("MISSED_OPP_TRACKING", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except BaseException:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config() -> Optional[dict[str, str]]:
    """League Supabase config, read at call time. None if not configured.

    Mirrors league_core.status._config, except bot_id is optional here:
    the table stores bot_name as plain text with a default, so a missing
    LEAGUE_BOT_ID degrades to "unattributed row" rather than "no row".
    """
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    if not url or not key:
        return None
    return {"url": url, "key": key, "bot_id": os.getenv("LEAGUE_BOT_ID", "")}


def _headers(cfg: dict[str, str], *, write: bool = False) -> dict[str, str]:
    h = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
    }
    if write:
        h["Content-Type"] = "application/json"
        h["Prefer"] = "return=minimal"
    return h


# ── Skip-reason bucketing ────────────────────────────────────────────────────


def classify_skip_reason(reason: str) -> str:
    """Map a free-text skip reason to a coarse, stable bucket.

    The bot's reason strings embed live numbers ("momentum rank=4
    score=0.0123, no breakout"), so grouping on the raw text yields one
    group per row. This produces something you can GROUP BY.

    Buckets are matched most-specific-first. Returns 'other' rather than
    guessing — an 'other' bucket that grows is a signal to add a rule here,
    which is a better failure than silently mislabelling.
    """
    try:
        r = (reason or "").lower()
        if "no momentum or breakout" in r or "no breakout" in r:
            return "no_signal"
        if "cooldown" in r:
            return "cooldown"
        if "open supabase position" in r:
            return "already_held"
        if "confidence" in r:
            return "low_confidence"
        if "insufficient bars" in r or "no price data" in r:
            return "insufficient_data"
        if "invalid price" in r or "invalid" in r:
            return "bad_data"
        if "minimum" in r or "alloc" in r:
            return "below_min_order"
        if "exposure" in r or "buying power" in r:
            return "no_capital"
        if "min_hold" in r:
            return "min_hold"
        return "other"
    except BaseException:
        return "other"


# ── Write path ───────────────────────────────────────────────────────────────


def safe_insert_missed_opportunity(
    *,
    symbol: str,
    skip_reason: str,
    bot_name: str = "stock_momentum_v1",
    run_id: Optional[str] = None,
    price: Optional[float] = None,
    signal_type: Optional[str] = None,
    signal_strength: Optional[float] = None,
    score: Optional[float] = None,
    regime: Optional[str] = None,
    rank: Optional[int] = None,
    indicators: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    timeout: float = 3.0,
) -> bool:
    """Record one rejected candidate. Returns True if the row was written.

    CALLED FROM INSIDE A LIVE TRADING LOOP. The return value exists for
    tests; the caller ignores it. Nothing this function does — including
    total failure — may affect the cycle.

    Timeout is 3s, tighter than league_core.status's 5s, because this runs
    once per rejected symbol and there can be a dozen per cycle. Analytics
    must not add measurable latency to a loop that places orders.
    """
    if not tracking_enabled():
        return False
    try:
        if not symbol or not skip_reason:
            return False
        cfg = _config()
        if cfg is None or requests is None:
            return False

        row: dict[str, Any] = {
            "bot_name":        str(bot_name or "unknown")[:120],
            "symbol":          str(symbol).upper()[:32],
            "skip_reason":     str(skip_reason)[:2000],
            "blocked_by_rule": classify_skip_reason(skip_reason),
            "indicators":      indicators or {},
            "metadata":        metadata or {},
        }
        if run_id:
            row["run_id"] = str(run_id)[:64]
        for key, val in (
            ("price", price),
            ("signal_strength", signal_strength),
            ("score", score),
        ):
            fv = _to_float(val)
            if fv is not None:
                row[key] = fv
        if regime:
            row["regime"] = str(regime)[:32]
        if signal_type:
            row["signal_type"] = str(signal_type)[:64]
        iv = _to_int(rank)
        if iv is not None:
            row["rank"] = iv

        resp = requests.post(
            f"{cfg['url']}/rest/v1/{TABLE}",
            headers=_headers(cfg, write=True),
            json=[row],
            timeout=timeout,
        )
        if resp.status_code >= 400:
            print(f"[missed_opps] insert {symbol}: HTTP {resp.status_code} "
                  f"{resp.text[:160]}")
            return False
        return True
    except BaseException as e:  # noqa: BLE001 - must never reach the caller
        print(f"[missed_opps] insert {symbol!r} failed (ignored): {e!r}")
        return False


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        # NaN != NaN. PostgREST rejects NaN, and a sentinel like -999.0 is
        # a real value we DO want recorded, so only NaN/inf are dropped.
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except BaseException:
        return None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except BaseException:
        return None


# ── Outcome scoring ──────────────────────────────────────────────────────────


def _fetch_unscored(cfg: dict[str, str], limit: int, timeout: float) -> list[dict[str, Any]]:
    """Rows with no outcome yet, oldest first, old enough to have one."""
    if requests is None:
        return []
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_HORIZON_MIN_CALENDAR_DAYS[1])).isoformat()
    url = (
        f"{cfg['url']}/rest/v1/{TABLE}"
        f"?outcome_checked_at=is.null"
        f"&created_at=lt.{cutoff}"
        f"&select=id,symbol,created_at,price"
        f"&order=created_at.asc&limit={int(limit)}"
    )
    try:
        resp = requests.get(url, headers=_headers(cfg), timeout=timeout)
        if resp.status_code >= 400:
            print(f"[missed_opps] fetch unscored: HTTP {resp.status_code}")
            return []
        rows = resp.json()
        return rows if isinstance(rows, list) else []
    except BaseException as e:  # noqa: BLE001
        print(f"[missed_opps] fetch unscored failed: {e!r}")
        return []


def _bar_date(bar: dict[str, Any]) -> Optional[str]:
    """YYYY-MM-DD for a Public bar, or None."""
    try:
        ts = bar.get("timestamp")
        if ts is None:
            return None
        if isinstance(ts, str):
            return ts[:10]
        # Public returns epoch values; milliseconds are ~1e12, seconds ~1e9.
        tsf = float(ts)
        if tsf > 1e11:
            tsf /= 1000.0
        return datetime.fromtimestamp(tsf, tz=timezone.utc).strftime("%Y-%m-%d")
    except BaseException:
        return None


def _forward_returns(
    bars: list[dict[str, Any]],
    created_at: str,
    recorded_price: Optional[float],
) -> tuple[dict[str, Optional[float]], bool]:
    """Forward returns from the skip date. Returns (returns, any_measured).

    UNITS: DECIMAL RETURNS, NOT PERCENT, rounded to 6 decimal places.

        106/105 - 1 = 0.009523809...  -> stored 0.009524  (= +0.95%)
        110/105 - 1 = 0.047619047...  -> stored 0.047619  (= +4.76%)

    Decimal is the finance convention and composes correctly under
    multiplication, which percentages do not. Callers that display these
    must multiply by 100. Do NOT scale at write time to make the numbers
    "look right" — 0.0095 IS +0.95%.

    The 6 dp rounding is deliberate: it keeps float noise out of the
    numeric column while preserving precision far finer than any return
    this is used to analyse (1e-6 = 0.0001%). Tests must compare against
    the ROUNDED value; asserting against the raw quotient with a tolerance
    tighter than 5e-7 will fail on correct output.

    Baseline is the close of the last bar ON OR BEFORE the skip date —
    which is why `price` being NULL at capture time is fine. A horizon
    stays None when there aren't enough bars after the baseline yet; the
    caller uses `any_measured` to decide whether to mark the row scored.
    """
    out: dict[str, Optional[float]] = {1: None, 5: None, 20: None}
    try:
        day = (created_at or "")[:10]
        if not day or not bars:
            return out, False

        dated = [(d, b) for b in bars if (d := _bar_date(b))]
        dated.sort(key=lambda t: t[0])

        base_idx = None
        for i, (d, _b) in enumerate(dated):
            if d <= day:
                base_idx = i
            else:
                break
        if base_idx is None:
            return out, False

        base_px = _to_float(dated[base_idx][1].get("close"))
        if base_px is None or base_px <= 0:
            base_px = _to_float(recorded_price)
        if base_px is None or base_px <= 0:
            return out, False

        measured = False
        for horizon in (1, 5, 20):
            tgt = base_idx + horizon
            if tgt < len(dated):
                px = _to_float(dated[tgt][1].get("close"))
                if px is not None and px > 0:
                    out[horizon] = round((px / base_px) - 1.0, 6)
                    measured = True
        return out, measured
    except BaseException as e:  # noqa: BLE001
        print(f"[missed_opps] forward-return calc failed: {e!r}")
        return out, False


def update_missed_opportunity_outcomes(
    *,
    limit: int = 200,
    timeout: float = 10.0,
) -> dict[str, int]:
    """Backfill forward returns on unscored rows. Never raises.

    Returns a counts dict for logging. Runs as its OWN scheduled job,
    separate from every trading job — the trading bots neither call this
    nor depend on it having run.

    A row is marked scored only once at least one horizon was measurable.
    Rows too recent for even the 1-day horizon are left untouched so a
    later pass can complete them, rather than being stamped with nulls.
    """
    stats = {"considered": 0, "updated": 0, "skipped_no_data": 0, "errors": 0}
    if not tracking_enabled():
        print("[missed_opps] MISSED_OPP_TRACKING is off; scorer is a no-op")
        return stats
    try:
        cfg = _config()
        if cfg is None or requests is None:
            print("[missed_opps] League Supabase not configured; scorer no-op")
            return stats

        rows = _fetch_unscored(cfg, limit, timeout)
        stats["considered"] = len(rows)
        if not rows:
            print("[missed_opps] no unscored rows due for scoring")
            return stats

        try:
            from league_core.public_bars import get_public_bars
        except BaseException as e:  # noqa: BLE001
            print(f"[missed_opps] public_bars unavailable; scorer aborted: {e!r}")
            stats["errors"] += 1
            return stats

        # One bar fetch per distinct symbol, not per row.
        bars_cache: dict[str, list[dict[str, Any]]] = {}

        for row in rows:
            try:
                sym = str(row.get("symbol") or "").upper()
                if not sym:
                    continue
                if sym not in bars_cache:
                    bars_cache[sym] = get_public_bars(sym, period="YEAR") or []
                bars = bars_cache[sym]
                if not bars:
                    stats["skipped_no_data"] += 1
                    continue

                rets, measured = _forward_returns(
                    bars, str(row.get("created_at") or ""), row.get("price"),
                )
                if not measured:
                    stats["skipped_no_data"] += 1
                    continue

                payload: dict[str, Any] = {"outcome_checked_at": _now_iso()}
                for horizon, col in ((1, "next_1d_return"),
                                     (5, "next_5d_return"),
                                     (20, "next_20d_return")):
                    if rets[horizon] is not None:
                        payload[col] = rets[horizon]

                resp = requests.patch(
                    f"{cfg['url']}/rest/v1/{TABLE}?id=eq.{row.get('id')}",
                    headers=_headers(cfg, write=True),
                    json=payload,
                    timeout=timeout,
                )
                if resp.status_code >= 400:
                    print(f"[missed_opps] update {sym}: HTTP {resp.status_code}")
                    stats["errors"] += 1
                else:
                    stats["updated"] += 1
            except BaseException as e:  # noqa: BLE001
                print(f"[missed_opps] row scoring failed (ignored): {e!r}")
                stats["errors"] += 1

        print(f"[missed_opps] scorer: {stats}")
        return stats
    except BaseException as e:  # noqa: BLE001
        print(f"[missed_opps] scorer failed (ignored): {e!r}")
        stats["errors"] += 1
        return stats


def main() -> int:
    """Entry point for the scheduled scorer job. Always exits 0.

    Exit 0 even on failure is deliberate: this is an optional analytics
    job, and a nonzero exit would mark the Fly machine's run as failed and
    pollute the health signal that real trading bots depend on.
    """
    update_missed_opportunity_outcomes()
    return 0


__all__ = [
    "tracking_enabled",
    "classify_skip_reason",
    "safe_insert_missed_opportunity",
    "update_missed_opportunity_outcomes",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
