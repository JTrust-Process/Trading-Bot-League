"""bots/agent_research_v1/context.py — gather recent platform state.

Pulls the last 24h of bot_research_scores, bot_signals, open positions,
and bot_events from the League Supabase project. Trims each list to the
most relevant rows so the prompt stays under a few KB of tokens.

This module does read-only HTTP. It does not write back to Supabase.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests


TIMEOUT = 8.0
LOOKBACK_HOURS = 24


def _config() -> Optional[dict[str, str]]:
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    if not url or not key:
        return None
    return {"url": url, "key": key}


def _iso_n_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _get(cfg: dict[str, str], path: str) -> List[Dict[str, Any]]:
    url = f"{cfg['url']}/rest/v1/{path}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"[agent.context] GET {path} failed: {e!r}")
        return []
    if resp.status_code >= 400:
        print(f"[agent.context] GET {path} status={resp.status_code} body={resp.text[:200]}")
        return []
    try:
        out = resp.json()
        return out if isinstance(out, list) else []
    except Exception as e:  # noqa: BLE001
        print(f"[agent.context] decode {path} failed: {e!r}")
        return []


def gather() -> Dict[str, Any]:
    """Return a single dict the prompt builder can flatten into the message.

    Shape:
      {
        "as_of":       ISO timestamp,
        "registry":    [ {bot_id, mode, bot_type}, ... ],
        "scores":      [ latest score per (bot, symbol) ],
        "signals":     [ recent signals, last 24h, newest first ],
        "positions":   [ open positions across the league ],
        "events":      [ recent events, last 24h ],
      }
    """
    cfg = _config()
    now = datetime.now(timezone.utc)
    out: Dict[str, Any] = {
        "as_of":     now.isoformat(),
        "registry":  [],
        "scores":    [],
        "signals":   [],
        "positions": [],
        "events":    [],
    }
    if cfg is None:
        return out

    cutoff = _iso_n_hours_ago(LOOKBACK_HOURS)

    # Registry — small and useful for the prompt
    reg = _get(cfg, "bot_registry?select=bot_id,bot_name,bot_type,mode,status&order=bot_id.asc")
    out["registry"] = [
        {k: r.get(k) for k in ("bot_id", "bot_name", "bot_type", "mode", "status")}
        for r in reg
    ]

    # Scores — latest 60 rows, then dedup to latest per (bot_id, symbol) client-side
    sc = _get(cfg, "bot_research_scores?select=*&order=scored_at.desc&limit=60")
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for s in sc:
        k = (s.get("bot_id"), s.get("symbol"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append({
            "bot_id":         s.get("bot_id"),
            "symbol":         s.get("symbol"),
            "asset_class":    s.get("asset_class"),
            "score":          s.get("score"),
            "classification": s.get("classification"),
            "notes":          s.get("notes"),
            "scored_at":      s.get("scored_at"),
        })
    out["scores"] = deduped

    # Signals — last 24h
    sg = _get(cfg, f"bot_signals?select=*&generated_at=gte.{cutoff}&order=generated_at.desc&limit=40")
    out["signals"] = [
        {
            "bot_id":      s.get("bot_id"),
            "symbol":      s.get("symbol"),
            "signal_type": s.get("signal_type"),
            "direction":   s.get("direction"),
            "confidence":  s.get("confidence"),
            "rationale":   s.get("rationale"),
            "generated_at": s.get("generated_at"),
            "strategy":    (s.get("metadata") or {}).get("strategy"),
        }
        for s in sg
    ]

    # Open positions
    ps = _get(cfg, "bot_positions?select=*&status=eq.open&order=entry_at.desc&limit=40")
    out["positions"] = [
        {
            "bot_id":       p.get("bot_id"),
            "symbol":       p.get("symbol"),
            "asset_class":  p.get("asset_class"),
            "quantity":     p.get("quantity"),
            "entry_price":  p.get("entry_price"),
            "amount_usd":   p.get("amount_usd"),
            "is_paper":     p.get("is_paper"),
            "direction":    (p.get("metadata") or {}).get("direction", "long"),
            "entry_at":     p.get("entry_at"),
        }
        for p in ps
    ]

    # Events — last 24h, useful for "what happened today"
    ev = _get(cfg, f"bot_events?select=bot_id,event_type,symbol,message,occurred_at&occurred_at=gte.{cutoff}&order=occurred_at.desc&limit=30")
    out["events"] = ev

    return out


def to_compact_json(ctx: Dict[str, Any]) -> str:
    """Serialize the context dict to compact JSON suitable for pasting into a prompt.
    Strips fields that are None to keep token count down."""
    def _strip(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _strip(v) for k, v in o.items() if v is not None}
        if isinstance(o, list):
            return [_strip(x) for x in o]
        return o
    return json.dumps(_strip(ctx), separators=(",", ":"), default=str)


__all__ = ["gather", "to_compact_json", "LOOKBACK_HOURS"]
