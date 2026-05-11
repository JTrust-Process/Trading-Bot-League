"""bots/etf_rotation_v1/state.py — tiny state.json read/write.

The bot's source of truth for paper positions is bot_positions in the
League Supabase project. This state file holds only:

  last_target_set:  list of symbols we were trying to be holding last cycle.
                    Used to detect regime changes between cycles.
  last_rebalance_at: ISO timestamp string. Helps the run-summary log.
  paper_capital:    Starting paper capital ($). Carries forward unchanged
                    across cycles; PnL is reflected in bot_positions rows.

We do not persist current per-symbol holdings here — that's what the
bot_positions table is for, and it survives even if state.json is lost
(GHA cache miss). state.json is purely a perf-friendly hint.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

STATE_FILE = "state.json"


def default_state(paper_capital: float) -> Dict[str, Any]:
    return {
        "last_target_set":   [],
        "last_rebalance_at": None,
        "paper_capital":     float(paper_capital),
    }


def load_state(default_capital: float) -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                # Backward-fill any missing keys with defaults.
                merged = default_state(default_capital)
                merged.update({k: data.get(k, merged[k]) for k in merged})
                # Coerce last_target_set to list of upper-case strings.
                lts = merged.get("last_target_set") or []
                merged["last_target_set"] = sorted({str(x).upper() for x in lts})
                return merged
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[etf_state] read failed: {e!r}")
    return default_state(default_capital)


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as e:  # noqa: BLE001
        print(f"[etf_state] write failed: {e!r}")


__all__ = ["STATE_FILE", "load_state", "save_state", "default_state"]
