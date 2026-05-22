"""bots/public_shadow_v1/main.py — entry point for the shadow logger.

Polls Public account #2's trade history and mirrors any new trades into
the League's `bot_trades` table under a virtual bot_id. Designed to be
fired by `agent_runner` on a short interval (every 10 minutes, 24/7).

NEVER places orders. Read-only against Public's API.

Env vars:
  PUBLIC_SECRET_ACCOUNT2      — Public API secret for the new account
  PUBLIC_ACCOUNT_ID_ACCOUNT2  — account ID for the new account
  LEAGUE_SUPABASE_URL, LEAGUE_SUPABASE_KEY — same as other bots
  LEAGUE_BOT_ID               — set by agent_runner to 'public_shadow_v1'

If either of the first two is missing, the bot logs and exits 0 — that's
fine. Useful so we can deploy this bot to Fly before account #2 is
configured.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from league_core import status as league
from bots.public_shadow_v1 import api


# Where to write the trades. Default to a single virtual bot ID for v1;
# we'll add per-tool attribution (claude_mcp / openclaw / perplexity) once
# multiple tools are active simultaneously and we have heuristics for
# splitting them.
DEFAULT_VIRTUAL_BOT_ID = os.getenv("PUBLIC_SHADOW_TARGET_BOT_ID", "public_account2_v1")


def _select_target_bot_id(_trade: Dict[str, Any]) -> str:
    """Attribute a trade to a virtual bot_id.

    v1: everything goes to a single bot_id. Future: split by time-of-day,
    instrument type, or other heuristics if user runs multiple AI tools
    on the same account simultaneously.
    """
    return DEFAULT_VIRTUAL_BOT_ID


def _ingest_trades(trades: List[Dict[str, Any]], run_id: Optional[str]) -> Dict[str, int]:
    """Insert each normalized trade into bot_trades.

    Uses log_trade() which posts with `upsert=False`, but the bot_trades
    table has a unique index on (bot_id, order_id) so duplicates are
    silently rejected by Postgres. That's the dedup mechanism — we
    don't track state on our side.
    """
    stats = {"submitted": 0, "skipped": 0}
    for raw in trades:
        normalized = api.normalize_trade(raw)
        if not normalized:
            stats["skipped"] += 1
            continue

        target_bot = _select_target_bot_id(normalized)

        # Temporarily swap LEAGUE_BOT_ID so log_trade writes under the
        # virtual bot ID for THIS trade. Restore afterward so subsequent
        # league_health/agent_runner logic isn't confused.
        prev = os.environ.get("LEAGUE_BOT_ID")
        os.environ["LEAGUE_BOT_ID"] = target_bot
        try:
            league.log_trade(
                symbol=normalized["symbol"],
                side=normalized["side"],
                asset_class=normalized["asset_class"],
                quantity=normalized["quantity"],
                price=normalized["price"],
                amount_usd=normalized["amount_usd"],
                fees_usd=normalized["fees_usd"] or 0.0,
                pnl_usd=None,       # Public's history doesn't give per-trade PnL directly
                pnl_pct=None,
                reason="public_shadow_mirror",
                strategy="public_ai",
                is_paper=False,     # account 2 trades ARE real money — just placed by AI tools
                order_id=normalized["order_id"],
                run_id=run_id,
                metadata={
                    "source": "public_shadow_v1",
                    "occurred_at_source": normalized["occurred_at"],
                },
            )
            stats["submitted"] += 1
        finally:
            if prev is None:
                os.environ.pop("LEAGUE_BOT_ID", None)
            else:
                os.environ["LEAGUE_BOT_ID"] = prev

    return stats


def run_cycle() -> int:
    """One polling cycle. Returns 0 on success, non-zero on hard error.

    Fail-silent: missing credentials = 0 (we want this deployable before
    account 2 is set up). API errors = 0 (try again next cycle).
    """
    league.heartbeat("healthy")
    run_id = league.start_run(trigger="agent_runner")

    try:
        # Secret: prefer account-2-specific, fall back to the shared
        # PUBLIC_SECRET (matches the fallback logic in api.get_access_token).
        secret_set = bool(os.getenv("PUBLIC_SECRET_ACCOUNT2")
                          or os.getenv("PUBLIC_SECRET"))
        account_set = bool(os.getenv("PUBLIC_ACCOUNT_ID_ACCOUNT2"))
        if not (secret_set and account_set):
            print("[public_shadow] credentials not yet configured "
                  f"(secret={secret_set}, account_id={account_set}); idle cycle")
            league.heartbeat("healthy")
            league.end_run(run_id, status="success", trade_count=0,
                           notes="no credentials configured yet")
            return 0

        history = api.fetch_history()
        if history is None:
            # Hard API error already logged; treat as a soft skip.
            league.heartbeat("degraded")
            league.end_run(run_id, status="success", trade_count=0,
                           notes="api fetch failed; will retry next cycle")
            return 0

        if not history:
            print("[public_shadow] no trades returned")
            league.heartbeat("healthy")
            league.end_run(run_id, status="success", trade_count=0,
                           notes="no trades in history window")
            return 0

        stats = _ingest_trades(history, run_id)
        print(f"[public_shadow] submitted={stats['submitted']} skipped={stats['skipped']}")
        league.heartbeat("healthy")
        league.end_run(run_id, status="success",
                       trade_count=stats["submitted"],
                       notes=f"submitted={stats['submitted']} skipped={stats['skipped']}")
        return 0

    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        league.heartbeat("down")
        league.end_run(run_id, status="failed", trade_count=0, error_count=1,
                       notes=f"exception: {e}")
        return 1


def main() -> int:
    return run_cycle()


if __name__ == "__main__":
    sys.exit(main())
