"""bots/short_watchlist_v1/state.py — tiny state file.

Source of truth for the bot's paper positions is bot_positions in the
League Supabase project. This state file holds:

  paper_short_capital_per_trade:  $ per simulated short — sizing input
  last_run_at:                    ISO timestamp, for visibility
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

# Anchor state.json to THIS file's directory rather than the process cwd.
# Same fix as etf_rotation_v1/state.py — without this, the workflow cache
# can't find the state file at `bots/short_watchlist_v1/state.json`.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def default_state(capital_per_trade: float) -> Dict[str, Any]:
    return {
        "paper_short_capital_per_trade": float(capital_per_trade),
        "last_run_at": None,
    }


def load_state(default_capital: float) -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                merged = default_state(default_capital)
                merged.update({k: data.get(k, merged[k]) for k in merged})
                return merged
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[short_state] read failed: {e!r}")
    return default_state(default_capital)


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as e:  # noqa: BLE001
        print(f"[short_state] write failed: {e!r}")


__all__ = ["STATE_FILE", "load_state", "save_state", "default_state"]
