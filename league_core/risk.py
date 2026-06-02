"""league_core/risk.py — preflight risk gate for any League bot placing real orders.

Every bot that is about to send an order to a real brokerage MUST call
`preflight()` immediately before placing the order. preflight returns
`(allowed, reason)`; if `allowed is False`, the order must not be placed.

Reason codes are short, stable strings (see the `REASON_*` constants below)
so they're greppable in logs and reusable as dashboard filters.

Design rules (PLAN.md §4 + this file's own conventions):

  * FAIL-CLOSED. If the League isn't configured, the registry row can't be
    fetched, or any input is questionable, we return (False, ...). This is
    the OPPOSITE of league_core.status, which is fail-silent for logging.
    Risk decisions cannot be silent — if we can't prove an order is safe,
    we refuse it.

  * NO SIDE EFFECTS. preflight does not write to any table. It does not
    log the refusal anywhere. The CALLER is responsible for logging
    (typically into bot_events with event_type='RISK_REFUSED'). This
    keeps the gate testable as a pure function and lets callers attach
    context (run_id, signal_id, etc.) the gate doesn't have.

  * LAZY ENV READS. Same pattern as league_core.status: read os.getenv at
    call time, never at import time, so callers that load_dotenv() in
    main() see fresh values.

  * MINIMAL DB CALLS. At most two GETs per preflight: one for the
    bot_registry row, one for today's trade count (only when a daily cap
    is set AND the action is an OPEN). Skip the second when not needed.

  * KILL SWITCH. LEAGUE_KILL=1 (or 'true'/'yes') in env stops every
    preflight in the process. Designed to be flippable on Fly without a
    full redeploy: `fly secrets set LEAGUE_KILL=1` then restart.

Recommended caller pattern (will be the shape used in etf_rotation_v1
once equities.py lands):

    from league_core import risk, status as league

    ok, reason = risk.preflight(action="BUY", symbol="SPY", amount_usd=50.0)
    if not ok:
        league.log_event(
            "RISK_REFUSED", symbol="SPY",
            message=reason, run_id=run_id,
            metadata={"action": "BUY", "amount_usd": 50.0},
        )
        return  # do NOT place the order

    # ...proceed to public_api.equities.place_order(...)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - present in every deployment
    requests = None  # type: ignore[assignment]


# ── Reason codes ─────────────────────────────────────────────────────────────
# Short, stable strings. Never reword these without grepping callers — they
# may be relied on by dashboard filters and alert rules.

REASON_OK                    = "ok"
REASON_KILL_GLOBAL           = "kill_switch_global"
REASON_KILL_BOT              = "kill_switch_bot"
REASON_BOT_UNKNOWN           = "bot_not_in_registry"
REASON_MODE_NOT_LIVE         = "bot_mode_not_live"
REASON_AGENT_RESEARCH        = "bot_type_agent_research_blocked"
REASON_INVALID_ACTION        = "action_invalid"
REASON_SYMBOL_NOT_ALLOWED    = "symbol_not_in_allowlist"
REASON_OVER_MAX_ORDER        = "amount_exceeds_max_order_usd"
REASON_DAILY_TRADES_CAP      = "daily_trades_cap_reached"
REASON_NO_LEAGUE             = "league_not_configured"
REASON_REGISTRY_FETCH_FAILED = "registry_fetch_failed"
REASON_TRADE_COUNT_FAILED    = "trade_count_fetch_failed"


# ── Action taxonomy ─────────────────────────────────────────────────────────
# OPEN actions create new exposure (and so are subject to daily-trade caps).
# CLOSE actions reduce exposure and skip the trade-count cap — an emergency
# exit should never be blocked by "too many trades today" since that would
# leave the bot stuck holding a position it wants to dump.

ALLOWED_ACTIONS = {"BUY", "SELL", "SHORT", "COVER"}
OPEN_ACTIONS    = {"BUY", "SHORT"}
CLOSE_ACTIONS   = {"SELL", "COVER"}


# ── Truthy-env helper ──────────────────────────────────────────────────────

def _is_truthy_env(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


# ── Config / IO ─────────────────────────────────────────────────────────────

def _config() -> Optional[dict[str, str]]:
    """Read env vars at call time. None if essentials are missing.

    Same shape as league_core.status._config so callers can swap freely.
    """
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    bot_id = os.getenv("LEAGUE_BOT_ID", "")
    if not url or not key or not bot_id:
        return None
    return {"url": url, "key": key, "bot_id": bot_id}


def _fetch_registry_row(cfg: dict[str, str], bot_id: str) -> Optional[dict[str, Any]]:
    """GET the bot_registry row. None on any failure (network, 4xx/5xx,
    bad JSON, missing row). Caller treats None as REASON_REGISTRY_FETCH_FAILED.
    """
    if requests is None:
        return None
    select = (
        "bot_id,bot_type,mode,status,allowed_instruments,"
        "can_place_orders,manual_approval_required,max_order_usd,"
        "max_daily_trades,max_open_positions"
    )
    url = (
        f"{cfg['url']}/rest/v1/bot_registry"
        f"?bot_id=eq.{bot_id}&select={select}&limit=1"
    )
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5.0)
    except Exception:  # noqa: BLE001 - fail-closed; caller refuses
        return None
    if resp.status_code >= 400:
        return None
    try:
        rows = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _count_today_trades(cfg: dict[str, str], bot_id: str) -> Optional[int]:
    """Count this bot's bot_trades rows since UTC midnight. None on failure.

    Uses PostgREST's `Prefer: count=exact` + `Range: 0-0` pattern: ask for
    zero rows, but request the total count in the Content-Range header.
    Cheap — Postgres counts via the index on (bot_id, occurred_at).
    """
    if requests is None:
        return None
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    iso = today_start.isoformat()
    url = (
        f"{cfg['url']}/rest/v1/bot_trades"
        f"?bot_id=eq.{bot_id}&occurred_at=gte.{iso}&select=id"
    )
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
        "Prefer": "count=exact",
        "Range-Unit": "items",
        "Range": "0-0",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5.0)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code not in (200, 206):
        return None
    cr = resp.headers.get("Content-Range") or ""
    if "/" not in cr:
        return None
    try:
        return int(cr.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


# ── Pure rules engine (no IO — fully unit-testable) ─────────────────────────

def _evaluate_rules(
    registry: dict[str, Any],
    *,
    action: str,
    symbol: str,
    amount_usd: float,
    daily_trade_count: Optional[int] = None,
    kill_env: Optional[str] = None,
) -> tuple[bool, str]:
    """Pure-function rules engine. Returns (allowed, reason_code).

    All inputs are explicit so this can be exercised in tests without any
    network or Supabase. The order of checks below matters: cheapest /
    most-fatal checks first. Each rule cites the PLAN.md §4.2 number.
    """
    # §4.2.1  Global kill switch
    if _is_truthy_env(kill_env):
        return (False, REASON_KILL_GLOBAL)

    # §4.2.2  Per-bot kill switch
    if (registry.get("status") or "").lower() != "enabled":
        return (False, REASON_KILL_BOT)

    # Sanity: action must be one we recognise
    if action not in ALLOWED_ACTIONS:
        return (False, REASON_INVALID_ACTION)

    # §4.2.3  Mode must be 'live' to place real orders. Paper / research
    # bots have no business reaching this gate — if they do, it's a bug
    # in the bot, not a runtime policy decision.
    if (registry.get("mode") or "").lower() != "live":
        return (False, REASON_MODE_NOT_LIVE)

    # §4.2.9 / §1.5  agent_research can NEVER place real orders. This is
    # the architectural guarantee that "AI agents may research, summarize,
    # score, and propose ideas, but they cannot bypass risk controls."
    if (registry.get("bot_type") or "").lower() == "agent_research":
        return (False, REASON_AGENT_RESEARCH)

    # §4.2.4  Symbol allowlist. Convention: empty array OR ['*'] = "any".
    allowed = registry.get("allowed_instruments") or []
    if isinstance(allowed, list) and allowed and "*" not in allowed:
        allowed_upper = {str(s).upper() for s in allowed}
        if (symbol or "").upper() not in allowed_upper:
            return (False, REASON_SYMBOL_NOT_ALLOWED)

    # §4.2.5  Max order USD. Applies to BOTH open and close orders — a
    # giant exit is just as much a risk-mgmt concern as a giant open.
    max_order = registry.get("max_order_usd")
    if max_order is not None:
        try:
            if float(amount_usd) > float(max_order):
                return (False, REASON_OVER_MAX_ORDER)
        except (TypeError, ValueError):
            # Fail-closed on non-numeric inputs.
            return (False, REASON_OVER_MAX_ORDER)

    # §4.2.6  Daily trade cap. Applies to OPEN actions only — see
    # CLOSE_ACTIONS comment near the top for why we never block exits.
    max_daily = registry.get("max_daily_trades")
    if (
        action in OPEN_ACTIONS
        and max_daily is not None
        and int(max_daily) > 0
        and daily_trade_count is not None
        and daily_trade_count >= int(max_daily)
    ):
        return (False, REASON_DAILY_TRADES_CAP)

    return (True, REASON_OK)


# ── Public entry point ──────────────────────────────────────────────────────

def preflight(
    action: str,
    symbol: str,
    amount_usd: float,
    *,
    bot_id: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Gate for any real-money order placement. Returns (allowed, reason).

    Args:
        action: 'BUY' | 'SELL' | 'SHORT' | 'COVER'. Anything else returns
                (False, REASON_INVALID_ACTION).
        symbol: ticker, case-insensitive.
        amount_usd: notional in dollars. Float-coerced; non-numeric refuses.
        bot_id: defaults to LEAGUE_BOT_ID. Override only for ops scripts.
        context: reserved for future extensions (signal_id, approval_id,
                 strategy hints). Not currently consumed but the keyword is
                 fixed so callers don't break when we add features.

    Returns (False, reason) for any of:
        - LEAGUE_SUPABASE_URL / LEAGUE_SUPABASE_KEY / LEAGUE_BOT_ID missing
        - LEAGUE_KILL=1 in env
        - bot_registry row missing or unfetchable
        - bot_registry.status != 'enabled'
        - bot_registry.mode != 'live'
        - bot_registry.bot_type == 'agent_research'
        - action not in {BUY, SELL, SHORT, COVER}
        - symbol not in allowed_instruments (when set explicitly)
        - amount_usd > max_order_usd
        - daily trade count >= max_daily_trades (OPEN actions only)
        - trade-count fetch failed when a cap is set (can't prove we're under)

    Returns (True, REASON_OK) only when every check passed.

    Does NOT write to any table. Caller is responsible for logging refusals
    (recommended: bot_events with event_type='RISK_REFUSED').
    """
    cfg = _config()
    if cfg is None:
        return (False, REASON_NO_LEAGUE)

    target_bot_id = bot_id or cfg["bot_id"]

    registry = _fetch_registry_row(cfg, target_bot_id)
    if registry is None:
        return (False, REASON_REGISTRY_FETCH_FAILED)
    if not registry.get("bot_id"):
        return (False, REASON_BOT_UNKNOWN)

    # Only fetch the trade count when both: a cap is set AND the action is
    # an OPEN. CLOSE actions skip the cap entirely (see OPEN_ACTIONS docstring).
    daily_trade_count: Optional[int] = None
    max_daily = registry.get("max_daily_trades")
    if (
        action in OPEN_ACTIONS
        and max_daily is not None
        and int(max_daily) > 0
    ):
        daily_trade_count = _count_today_trades(cfg, target_bot_id)
        if daily_trade_count is None:
            # Can't count → can't prove we're under cap → fail-closed.
            return (False, REASON_TRADE_COUNT_FAILED)

    return _evaluate_rules(
        registry,
        action=action,
        symbol=symbol,
        amount_usd=float(amount_usd) if amount_usd is not None else float("inf"),
        daily_trade_count=daily_trade_count,
        kill_env=os.getenv("LEAGUE_KILL"),
    )


__all__ = [
    # Entry point
    "preflight",
    # Reason codes (stable, greppable)
    "REASON_OK",
    "REASON_KILL_GLOBAL",
    "REASON_KILL_BOT",
    "REASON_BOT_UNKNOWN",
    "REASON_MODE_NOT_LIVE",
    "REASON_AGENT_RESEARCH",
    "REASON_INVALID_ACTION",
    "REASON_SYMBOL_NOT_ALLOWED",
    "REASON_OVER_MAX_ORDER",
    "REASON_DAILY_TRADES_CAP",
    "REASON_NO_LEAGUE",
    "REASON_REGISTRY_FETCH_FAILED",
    "REASON_TRADE_COUNT_FAILED",
    # Action taxonomy (for callers that want to introspect)
    "ALLOWED_ACTIONS",
    "OPEN_ACTIONS",
    "CLOSE_ACTIONS",
]
