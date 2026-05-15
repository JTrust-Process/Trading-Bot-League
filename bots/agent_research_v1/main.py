"""bots/agent_research_v1/main.py — entry point.

GitHub Actions runs this once per scheduled trigger. The bot:

  1. Starts a League run.
  2. Gathers the last 24h of league state (registry, scores, signals,
     positions, events) via context.gather().
  3. Sends it to Claude with a structured system prompt that constrains
     output to JSON.
  4. Parses the response into a brief + observations + 0..N proposals.
  5. Writes the brief as an AGENT_BRIEF event in bot_events.
  6. Writes each proposal as TWO rows: a bot_signals row (signal_type=
     'agent_proposal', approval_required=true) and a bot_approvals row
     (status='pending'). The dashboard's Pending approvals section
     surfaces these for the human reviewer.
  7. Ends the run.

NEVER places a trade. NEVER imports an order client. NEVER writes
bot_trades or bot_positions. The bot's only Supabase write surface is
bot_runs, bot_status, bot_events, bot_signals, bot_approvals.

Exits 0 even if the LLM call failed — the schedule should keep trying.
"""

from __future__ import annotations

import json
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
from bots.agent_research_v1 import context as ctx_mod
from bots.agent_research_v1 import llm
from bots.agent_research_v1 import prompts


# ── Config ──────────────────────────────────────────────────────────────────


# Cap on proposals we'll actually accept from the LLM, regardless of what
# it returns. Defense in depth: if the LLM ignores "0-3 proposals" we
# truncate here.
MAX_PROPOSALS = int(os.getenv("AGENT_MAX_PROPOSALS", "3"))

# Actions the LLM may propose. Anything outside this set is rejected by
# the parser — the bot will not write the row to bot_approvals.
ALLOWED_ACTIONS = {
    "PAPER_BUY", "PAPER_SELL", "PAPER_SHORT", "PAPER_COVER",
    "OPTION_OPEN", "OPTION_CLOSE",
    "WATCH",          # "no action, just look at this" — useful for nudges
}

# Strategy strings we'll accept; anything else gets a warning and a clamp.
# We don't whitelist tightly — the LLM is allowed to invent reasonable
# strategy names — but flagrantly off shapes (naked_call, naked_put,
# margin_short, leveraged_long) get refused.
DENY_STRATEGY_SUBSTRINGS = (
    "naked", "margin", "leveraged", "concentrated",
)


def _coerce_proposal(prop: Any) -> Optional[Dict[str, Any]]:
    """Validate one proposal dict from the LLM. Returns the sanitized version
    or None if it should be dropped."""
    if not isinstance(prop, dict):
        return None
    symbol     = str(prop.get("symbol") or "").strip().upper()[:16]
    action     = str(prop.get("action") or "").strip().upper()
    strategy   = str(prop.get("strategy") or "").strip().lower()[:64]
    rationale  = str(prop.get("rationale") or "").strip()[:800]
    risk       = str(prop.get("risk") or "").strip()[:300]
    try:
        confidence = float(prop.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    if not symbol or not action or not strategy:
        print(f"[agent.main] proposal missing required fields: {prop!r}")
        return None
    if action not in ALLOWED_ACTIONS:
        print(f"[agent.main] proposal rejected — unrecognized action={action!r}")
        return None
    for bad in DENY_STRATEGY_SUBSTRINGS:
        if bad in strategy:
            print(f"[agent.main] proposal rejected — denied strategy={strategy!r}")
            return None

    return {
        "symbol":     symbol,
        "action":     action,
        "strategy":   strategy,
        "rationale":  rationale,
        "risk":       risk,
        "confidence": confidence,
    }


def _coerce_response(obj: Any) -> Dict[str, Any]:
    """Turn the LLM's JSON object into our normalized internal shape.
    Missing/malformed fields default to safe empty values."""
    if not isinstance(obj, dict):
        return {"brief": "", "observations": [], "proposals": []}

    brief = str(obj.get("brief") or "").strip()[:2000]
    observations_raw = obj.get("observations") or []
    if not isinstance(observations_raw, list):
        observations_raw = []
    observations = [str(o).strip()[:400] for o in observations_raw if str(o).strip()]
    observations = observations[:10]

    proposals_raw = obj.get("proposals") or []
    if not isinstance(proposals_raw, list):
        proposals_raw = []
    proposals: List[Dict[str, Any]] = []
    for p in proposals_raw:
        coerced = _coerce_proposal(p)
        if coerced:
            proposals.append(coerced)
        if len(proposals) >= MAX_PROPOSALS:
            break

    return {"brief": brief, "observations": observations, "proposals": proposals}


def run_cycle() -> str:
    final_status = "success"
    error_count = 0

    run_id = league.start_run("cron")
    print(f"[agent] league run_id={run_id}")

    try:
        # 1. Gather state.
        snapshot = ctx_mod.gather()
        n_reg = len(snapshot.get("registry", []))
        n_sc  = len(snapshot.get("scores", []))
        n_sg  = len(snapshot.get("signals", []))
        n_ps  = len(snapshot.get("positions", []))
        n_ev  = len(snapshot.get("events", []))
        print(f"[agent] context: registry={n_reg} scores={n_sc} signals={n_sg} "
              f"positions={n_ps} events={n_ev}")

        if n_reg == 0:
            print("[agent] empty registry — bot can't run without context.")
            league.log_event(
                "AGENT_SKIPPED",
                message="Empty registry; cannot brief.",
                run_id=run_id,
            )
            return "warning"

        compact = ctx_mod.to_compact_json(snapshot)
        # Sanity-cap the prompt size. Most days this is ~2-5KB, but a
        # noisy day could push it bigger; we truncate before sending.
        MAX_CONTEXT_CHARS = 24_000
        if len(compact) > MAX_CONTEXT_CHARS:
            compact = compact[:MAX_CONTEXT_CHARS] + " ...(truncated)"

        # 2. Call the LLM.
        user_msg = prompts.build_user_prompt(compact)
        raw = llm.call(system=prompts.SYSTEM_PROMPT, user=user_msg)
        if raw is None:
            print("[agent] LLM call failed; skipping write-back.")
            final_status = "warning"
            error_count += 1
            return final_status

        # 3. Parse.
        parsed = llm.extract_json_block(raw)
        if parsed is None:
            print(f"[agent] could not parse JSON from LLM response. Raw head: {raw[:200]!r}")
            final_status = "warning"
            error_count += 1
            # Record the raw text so a human can inspect what went wrong.
            league.log_event(
                "AGENT_PARSE_FAILED",
                message=raw[:400],
                metadata={"raw_head": raw[:1000]},
                run_id=run_id,
            )
            return final_status

        result = _coerce_response(parsed)
        brief = result["brief"]
        observations = result["observations"]
        proposals = result["proposals"]
        print(f"[agent] brief: {brief[:200]}")
        print(f"[agent] observations: {len(observations)}")
        print(f"[agent] proposals (after sanitization): {len(proposals)}")

        # 4. Write the brief as an AGENT_BRIEF event.
        league.log_event(
            "AGENT_BRIEF",
            message=brief or "(no narrative)",
            metadata={
                "observations":   observations,
                "proposal_count": len(proposals),
                "model":          os.getenv("AGENT_MODEL", llm.DEFAULT_MODEL),
                "context_chars":  len(compact),
            },
            run_id=run_id,
        )

        # 5. For each proposal, write BOTH a signal and a pending approval.
        #    The signal is the audit trail. The approval is what surfaces
        #    in the dashboard's Pending approvals section.
        for p in proposals:
            # bot_signals row (informational)
            signal_id: Optional[str] = None
            try:
                # We don't get the inserted signal id back today — the
                # adapter only returns it for start_run. Future enhancement
                # would have log_signal return the id so we can link the
                # approval to it. For now, signal_id stays null on the
                # approval row.
                league.log_signal(
                    signal_type="agent_proposal",
                    symbol=p["symbol"],
                    direction="NEUTRAL",
                    confidence=p["confidence"],
                    rationale=p["rationale"],
                    source="agent:claude",
                    approval_required=True,
                    metadata={
                        "action":   p["action"],
                        "strategy": p["strategy"],
                        "risk":     p["risk"],
                    },
                    run_id=run_id,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[agent] log_signal failed for {p['symbol']}: {e!r}")
                error_count += 1

            # bot_approvals row (pending — human gates execution)
            try:
                league.request_approval(
                    action=p["action"],
                    symbol=p["symbol"],
                    payload={
                        "strategy":   p["strategy"],
                        "rationale":  p["rationale"],
                        "risk":       p["risk"],
                        "confidence": p["confidence"],
                        "source":     "agent:claude",
                    },
                    signal_id=signal_id,
                    run_id=run_id,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[agent] request_approval failed for {p['symbol']}: {e!r}")
                error_count += 1

        if error_count > 0:
            final_status = "warning"
        return final_status

    except Exception:  # noqa: BLE001
        traceback.print_exc()
        final_status = "failed"
        error_count += 1
        return final_status

    finally:
        try:
            league.end_run(
                run_id=run_id,
                status=final_status,
                trade_count=0,   # this bot never trades
                error_count=error_count,
            )
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    status = run_cycle()
    print(f"[agent] cycle status={status}")
    sys.exit(0)
