"""league_core.status — heartbeat / start_run / end_run helpers.

Talks to the League Supabase project via PostgREST using `requests`. Chosen
over `supabase-py` so we have zero new mandatory dependencies — both
existing bots already have `requests` in their requirements files.

Design rules baked in (PLAN.md §8):

  * FAIL-SILENT. If LEAGUE_SUPABASE_URL is missing, any function in here
    is a no-op. If a network call fails, we print and return. The existing
    bot must continue to behave exactly as today even if the League
    project is unreachable.
  * Lazy env reads. Never read os.getenv at import time — the existing
    bots call load_dotenv() in main(), and any module-level os.getenv()
    would fire before that and grab stale values. Mirrors the pattern in
    the existing crypto_bot/config/settings.py.
  * No raises. Every function catches BaseException at the boundary.
  * Idempotent for upserts. bot_status uses Supabase's
    `Prefer: resolution=merge-duplicates` so repeated heartbeats just
    overwrite the row instead of erroring on the primary-key conflict.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - both bots have requests already
    requests = None  # type: ignore[assignment]


# ── Internal helpers ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config() -> Optional[dict[str, str]]:
    """Read env vars at call time. Return None if essentials are missing.

    A missing LEAGUE_SUPABASE_URL is the explicit signal that the League is
    not configured for this bot — every helper in this module short-circuits
    to a no-op in that case. This is what makes adoption optional and safe
    to roll out incrementally.
    """
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    bot_id = os.getenv("LEAGUE_BOT_ID", "")
    if not url or not key or not bot_id:
        return None
    return {"url": url, "key": key, "bot_id": bot_id}


def _print(msg: str) -> None:
    """Single place to log to stdout so output is greppable in GHA logs."""
    print(f"[league] {msg}", file=sys.stdout, flush=True)


def _post(
    cfg: dict[str, str],
    table: str,
    rows: list[dict[str, Any]],
    *,
    upsert: bool = False,
    return_representation: bool = False,
    timeout: float = 5.0,
) -> Optional[list[dict[str, Any]]]:
    """POST to PostgREST. Returns the response rows on success, None otherwise.

    Catches every exception. Never re-raises.
    """
    if requests is None:
        _print("requests not installed; skipping write")
        return None
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    prefer_parts: list[str] = []
    if upsert:
        prefer_parts.append("resolution=merge-duplicates")
    if return_representation:
        prefer_parts.append("return=representation")
    else:
        prefer_parts.append("return=minimal")
    if prefer_parts:
        headers["Prefer"] = ",".join(prefer_parts)

    url = f"{cfg['url']}/rest/v1/{table}"
    try:
        resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=timeout)
    except Exception as e:  # noqa: BLE001 - we want every failure to be silent
        _print(f"POST {table} failed: {e!r}")
        return None
    if resp.status_code >= 400:
        _print(f"POST {table} status={resp.status_code} body={resp.text[:200]}")
        return None
    if return_representation:
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            _print(f"POST {table} json decode failed: {e!r}")
            return None
    return []


def _patch(
    cfg: dict[str, str],
    table: str,
    filter_qs: str,
    payload: dict[str, Any],
    *,
    timeout: float = 5.0,
) -> bool:
    """PATCH to PostgREST. Returns True on success, False otherwise."""
    if requests is None:
        return False
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    url = f"{cfg['url']}/rest/v1/{table}?{filter_qs}"
    try:
        resp = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        _print(f"PATCH {table} failed: {e!r}")
        return False
    if resp.status_code >= 400:
        _print(f"PATCH {table} status={resp.status_code} body={resp.text[:200]}")
        return False
    return True


# ── Public API ──────────────────────────────────────────────────────────────


def heartbeat(
    health: str = "healthy",
    details: Optional[dict[str, Any]] = None,
    *,
    last_run_id: Optional[str] = None,
    last_run_status: Optional[str] = None,
    last_error_msg: Optional[str] = None,
) -> None:
    """Upsert this bot's row in bot_status.

    Safe to call any number of times per cycle. The first call after a
    cycle starts and one final call before exit is the recommended cadence
    for the existing bots in Step 1b.
    """
    cfg = _config()
    if cfg is None:
        return  # League not configured — no-op
    if health not in ("healthy", "degraded", "down", "unknown", "muted"):
        _print(f"heartbeat: invalid health={health!r}; coercing to 'unknown'")
        health = "unknown"
    row: dict[str, Any] = {
        "bot_id": cfg["bot_id"],
        "last_heartbeat_at": _now_iso(),
        "current_mode": os.getenv("LEAGUE_BOT_MODE") or None,
        "health": health,
        "details": details or {},
        "updated_at": _now_iso(),
    }
    if last_run_id is not None:
        row["last_run_id"] = last_run_id
    if last_run_status is not None:
        row["last_run_status"] = last_run_status
    if last_error_msg is not None:
        row["last_error_at"] = _now_iso()
        row["last_error_msg"] = last_error_msg[:500]
    _post(cfg, "bot_status", [row], upsert=True)


def start_run(trigger: str = "cron", git_sha: Optional[str] = None) -> Optional[str]:
    """Insert a new bot_runs row with status='running'. Returns the run_id (uuid).

    Returns None if the League isn't configured or the insert fails. Callers
    should treat None as "no League run to update later" and proceed normally.
    """
    cfg = _config()
    if cfg is None:
        return None
    run_id = str(uuid.uuid4())
    row = {
        "id": run_id,
        "bot_id": cfg["bot_id"],
        "started_at": _now_iso(),
        "status": "running",
        "trigger": trigger,
        "git_sha": git_sha or os.getenv("GITHUB_SHA"),
    }
    rows = _post(cfg, "bot_runs", [row], return_representation=True)
    if rows is None:
        return None
    # Heartbeat alongside the run row so the dashboard sees liveness even if
    # this is the first ever run of a freshly-registered bot.
    heartbeat(health="healthy", last_run_id=run_id, last_run_status="running")
    return run_id


def end_run(
    run_id: Optional[str],
    status: str = "success",
    *,
    trade_count: int = 0,
    error_count: int = 0,
    notes: Optional[str] = None,
) -> None:
    """Close out a bot_runs row.

    Safe to call with run_id=None (no-op) — this matches the existing
    crypto bot's pattern where start_run can fail and main.py still runs
    the finally block.
    """
    cfg = _config()
    if cfg is None or run_id is None:
        # Still emit a final heartbeat in the no-run-id case so the dashboard
        # at least sees a fresh timestamp even if we never opened a run.
        if cfg is not None:
            heartbeat(health="healthy" if status == "success" else "degraded")
        return
    if status not in ("running", "success", "warning", "failed", "timeout"):
        _print(f"end_run: invalid status={status!r}; coercing to 'warning'")
        status = "warning"
    payload = {
        "ended_at": _now_iso(),
        "status": status,
        "trade_count": trade_count,
        "error_count": error_count,
    }
    if notes is not None:
        payload["notes"] = notes[:1000]
    _patch(cfg, "bot_runs", f"id=eq.{run_id}", payload)
    # Final heartbeat reflects the run outcome so the cross-bot dashboard is
    # accurate the moment the cycle ends, without waiting for the next run.
    health = {
        "success": "healthy",
        "warning": "degraded",
        "failed": "down",
        "timeout": "down",
        "running": "healthy",
    }.get(status, "unknown")
    heartbeat(
        health=health,
        last_run_id=run_id,
        last_run_status=status,
        details={"trade_count": trade_count, "error_count": error_count},
    )


# ── Convenience: a single-call wrapper for cron-style bots ──────────────────


def quick_heartbeat(label: str = "alive") -> None:
    """Very thin convenience wrapper for callers that just want to ping.

    Used by health-only or smoke-test workflows.
    """
    heartbeat(health="healthy", details={"label": label, "ts": _now_iso()})


# ── Trade / event mirror ────────────────────────────────────────────────────


def log_trade(
    symbol: str,
    side: str,
    asset_class: str,
    *,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    amount_usd: Optional[float] = None,
    fees_usd: float = 0.0,
    pnl_usd: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    reason: Optional[str] = None,
    strategy: Optional[str] = None,
    is_paper: bool = False,
    order_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Insert a row into bot_trades. Fail-silent. asset_class must match the
    CHECK constraint: 'equity'|'etf'|'crypto'|'bond'|'option'|'option_spread'.
    """
    cfg = _config()
    if cfg is None:
        return
    side_u = (side or "").upper()
    if side_u not in ("BUY", "SELL", "SHORT", "COVER"):
        _print(f"log_trade: invalid side={side!r}; skipping")
        return
    row: dict[str, Any] = {
        "bot_id":      cfg["bot_id"],
        "run_id":      run_id,
        "occurred_at": _now_iso(),
        "symbol":      symbol,
        "asset_class": asset_class,
        "side":        side_u,
        "quantity":    quantity,
        "price":       price,
        "amount_usd":  amount_usd,
        "fees_usd":    fees_usd or 0,
        "pnl_usd":     pnl_usd,
        "pnl_pct":     pnl_pct,
        "reason":      reason,
        "strategy":    strategy,
        "is_paper":    bool(is_paper),
        "order_id":    order_id,
        "metadata":    metadata or {},
    }
    _post(cfg, "bot_trades", [row], upsert=False)


def log_event(
    event_type: str,
    *,
    symbol: Optional[str] = None,
    message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """Insert a row into bot_events. Fail-silent."""
    cfg = _config()
    if cfg is None:
        return
    row: dict[str, Any] = {
        "bot_id":     cfg["bot_id"],
        "run_id":     run_id,
        "occurred_at": _now_iso(),
        "event_type": event_type,
        "symbol":     symbol,
        "message":    message,
        "metadata":   metadata or {},
    }
    _post(cfg, "bot_events", [row], upsert=False)


def upsert_position(
    symbol: str,
    asset_class: str,
    *,
    status: str = "open",
    quantity: Optional[float] = None,
    entry_price: Optional[float] = None,
    entry_at: Optional[str] = None,
    amount_usd: Optional[float] = None,
    exit_price: Optional[float] = None,
    exit_at: Optional[str] = None,
    pnl_usd: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    close_reason: Optional[str] = None,
    is_paper: bool = False,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Insert OR update a bot_positions row. NOT a true upsert at the DB level
    because (bot_id, symbol) has a partial unique index only on status='open' —
    so we do read-then-write: pick the existing open row for this symbol or
    insert a fresh one. Fail-silent."""
    cfg = _config()
    if cfg is None:
        return
    # Find existing open row for this (bot_id, symbol). Closed positions
    # accumulate as historical records and we don't reopen them.
    if status == "open":
        existing = _get_open_position(cfg, symbol)
        if existing is not None:
            patch: dict[str, Any] = {
                "quantity":   quantity,
                "entry_price": entry_price,
                "amount_usd": amount_usd,
                "metadata":   metadata or {},
            }
            if entry_at is not None:
                patch["entry_at"] = entry_at
            _patch(cfg, "bot_positions",
                   f"id=eq.{existing['id']}", patch)
            return
    row: dict[str, Any] = {
        "bot_id":       cfg["bot_id"],
        "symbol":       symbol,
        "asset_class":  asset_class,
        "status":       status,
        "quantity":     quantity,
        "entry_price":  entry_price,
        "entry_at":     entry_at or _now_iso(),
        "amount_usd":   amount_usd,
        "exit_price":   exit_price,
        "exit_at":      exit_at,
        "pnl_usd":      pnl_usd,
        "pnl_pct":      pnl_pct,
        "close_reason": close_reason,
        "is_paper":     bool(is_paper),
        "metadata":     metadata or {},
    }
    _post(cfg, "bot_positions", [row], upsert=False)


def close_position(
    symbol: str,
    *,
    exit_price: Optional[float] = None,
    exit_at: Optional[str] = None,
    pnl_usd: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    close_reason: Optional[str] = None,
) -> None:
    """Flip the open bot_positions row for (bot_id, symbol) to status='closed'.
    No-op if no open row exists. Fail-silent."""
    cfg = _config()
    if cfg is None:
        return
    existing = _get_open_position(cfg, symbol)
    if existing is None:
        return
    patch = {
        "status":       "closed",
        "exit_price":   exit_price,
        "exit_at":      exit_at or _now_iso(),
        "pnl_usd":      pnl_usd,
        "pnl_pct":      pnl_pct,
        "close_reason": close_reason,
    }
    _patch(cfg, "bot_positions", f"id=eq.{existing['id']}", patch)


def _get_open_position(cfg: dict[str, str], symbol: str) -> Optional[dict[str, Any]]:
    """Fetch the open bot_positions row for (bot_id, symbol) or None."""
    if requests is None:
        return None
    url = (
        f"{cfg['url']}/rest/v1/bot_positions"
        f"?bot_id=eq.{cfg['bot_id']}&symbol=eq.{symbol}&status=eq.open"
        f"&select=id,quantity,entry_price,amount_usd,metadata&limit=1"
    )
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5.0)
    except Exception as e:  # noqa: BLE001
        _print(f"GET bot_positions failed: {e!r}")
        return None
    if resp.status_code >= 400:
        _print(f"GET bot_positions status={resp.status_code} body={resp.text[:200]}")
        return None
    try:
        rows = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def request_approval(
    action: str,
    *,
    symbol: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    signal_id: Optional[str] = None,
    expires_in_minutes: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a pending row into bot_approvals. Fail-silent. Returns the
    new approval id on success, None otherwise.

    Writing here does NOT execute anything. The dashboard's pending-
    approvals queue shows the row; a human Approve / Reject is what
    flips it to status='approved' (and a downstream execution bot only
    consumes 'approved' rows and flips them to 'consumed' after acting).

    action: short verb like 'BUY', 'SELL', 'SHORT', 'COVER',
            'OPTION_OPEN', 'OPTION_CLOSE'. Used by the dashboard for
            display and by future execution bots for routing.
    payload: full proposed order parameters as JSON.
    signal_id: optional link back to bot_signals.id.
    expires_in_minutes: if set, the row auto-expires for execution purposes
                        after this many minutes from now (the bot consumer
                        checks expires_at; the DB doesn't enforce it).
    """
    cfg = _config()
    if cfg is None:
        return None
    expires_at: Optional[str] = None
    if expires_in_minutes is not None and expires_in_minutes > 0:
        from datetime import datetime, timedelta, timezone
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=int(expires_in_minutes))
        ).isoformat()
    row: dict[str, Any] = {
        "bot_id":       cfg["bot_id"],
        "signal_id":    signal_id,
        "requested_at": _now_iso(),
        "expires_at":   expires_at,
        "action":       action,
        "symbol":       symbol,
        "payload":      payload or {},
        "status":       "pending",
    }
    rows = _post(cfg, "bot_approvals", [row], return_representation=True)
    if rows and isinstance(rows, list) and isinstance(rows[0], dict):
        ap_id = rows[0].get("id")
        return str(ap_id) if ap_id is not None else None
    return None


def log_signal(
    signal_type: str,
    *,
    symbol: Optional[str] = None,
    asset_class: Optional[str] = None,
    direction: Optional[str] = None,
    confidence: Optional[float] = None,
    suggested_size_usd: Optional[float] = None,
    rationale: Optional[str] = None,
    source: Optional[str] = None,
    approval_required: bool = False,
    metadata: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """Insert a row into bot_signals. Fail-silent.

    direction must be one of: 'LONG' | 'SHORT' | 'NEUTRAL' | 'EXIT' or None.
    asset_class must match the CHECK constraint when supplied.

    Writing a signal does NOT execute anything. It's purely informational —
    a human or another bot may later read it and decide to act.
    """
    cfg = _config()
    if cfg is None:
        return
    if direction is not None and direction not in ("LONG", "SHORT", "NEUTRAL", "EXIT"):
        _print(f"log_signal: invalid direction={direction!r}; writing as null")
        direction = None
    row: dict[str, Any] = {
        "bot_id":             cfg["bot_id"],
        "run_id":              run_id,
        "generated_at":        _now_iso(),
        "symbol":              symbol,
        "asset_class":         asset_class,
        "signal_type":         signal_type,
        "direction":           direction,
        "confidence":          confidence,
        "suggested_size_usd":  suggested_size_usd,
        "rationale":           rationale,
        "source":              source,
        "approval_required":   bool(approval_required),
        "metadata":            metadata or {},
    }
    _post(cfg, "bot_signals", [row], upsert=False)


def log_research_score(
    symbol: str,
    asset_class: str,
    *,
    score: Optional[float] = None,
    classification: Optional[str] = None,
    period: Optional[str] = None,
    metrics: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    """Insert a row into bot_research_scores. Fail-silent.

    classification must be one of: 'keep_active' | 'reduce_priority' |
    'paper_only' | 'remove' (matches the stock bot's analyze_backtests.py
    buckets) or None. asset_class must match the CHECK constraint.
    """
    cfg = _config()
    if cfg is None:
        return
    if classification is not None and classification not in (
        "keep_active", "reduce_priority", "paper_only", "remove"
    ):
        _print(f"log_research_score: invalid classification={classification!r}; "
               f"writing as null")
        classification = None
    row: dict[str, Any] = {
        "bot_id":         cfg["bot_id"],
        "run_id":         run_id,
        "scored_at":      _now_iso(),
        "symbol":         symbol,
        "asset_class":    asset_class,
        "period":         period,
        "score":          score,
        "classification": classification,
        "metrics":        metrics or {},
        "notes":          notes,
    }
    _post(cfg, "bot_research_scores", [row], upsert=False)


__all__ = [
    "heartbeat",
    "start_run",
    "end_run",
    "quick_heartbeat",
    "log_trade",
    "log_event",
    "upsert_position",
    "close_position",
    "log_research_score",
    "log_signal",
    "request_approval",
]
