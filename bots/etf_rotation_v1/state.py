"""bots/etf_rotation_v1/state.py — derive runtime state from durable sources.

There is no longer a state.json file. State that used to live there:

  last_target_set:   derived from bot_positions in the League Supabase
                     project (the ETF symbols we currently have open).
  last_rebalance_at: informational only; was never load-bearing. If you
                     want it, query bot_runs / bot_trades.
  paper_capital:     read from ETF_PAPER_CAPITAL env on every cycle
                     (defaults to 1000.0).

This module is deliberately tiny now. The previous file-based design lost
state on every container restart, which produced 28 phantom rebalances
between 2026-05-21 15:33 and 2026-05-22 14:33 (those bot_trades rows are
tagged `metadata.phantom=true` for filtering). The fix is to remove the
file dependency entirely: bot_positions is the durable source of truth,
and the bot derives target-set membership from it on every cycle.

`load_state` keeps its original signature so `main.py` needs no change.
`save_state` is a deliberate no-op (kept so existing call sites compile).
"""

from __future__ import annotations

import os
from typing import Any, Dict

from league_core import status as league


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def default_state(paper_capital: float) -> Dict[str, Any]:
    """Shape of the state dict main.py expects. Kept for back-compat with
    any caller that explicitly imports it."""
    return {
        "last_target_set":   [],
        "last_rebalance_at": None,
        "paper_capital":     float(paper_capital),
    }


def load_state(default_capital: float) -> Dict[str, Any]:
    """Derive the current runtime state from durable sources. Returns the
    same shape main.py used to read from state.json:

        {
          "last_target_set":   list[str]  # sorted, uppercase
          "last_rebalance_at": None
          "paper_capital":     float
        }

    `last_target_set` comes from bot_positions filtered to status='open'
    AND asset_class='etf' for this bot. If Supabase is unreachable
    (`league.get_open_symbols` returns None), we degrade to an empty list
    — the bot then treats the cycle as 'no prior positions', same self-
    heal behavior as the old file-based design. The crucial difference
    is that this branch now only triggers on a real Supabase failure,
    not on a routine container restart.
    """
    paper_capital = _env_float("ETF_PAPER_CAPITAL", default_capital)
    open_symbols = league.get_open_symbols(asset_class="etf")
    if open_symbols is None:
        # League unreachable / unconfigured. Empty target = "no prior
        # positions" — if regime calls for a basket, the cycle will
        # open it. Same recovery semantics as before, just rarer.
        last_target_set: list[str] = []
    else:
        last_target_set = open_symbols
    return {
        "last_target_set":   last_target_set,
        "last_rebalance_at": None,
        "paper_capital":     paper_capital,
    }


def save_state(state: Dict[str, Any]) -> None:
    """No-op. State is derived from Supabase on every cycle; there is
    nothing to persist locally. Kept so the existing call site in
    main.py (`bot_state.save_state(s)`) continues to work without edit.

    If you ever want to log rebalance timestamps for analytics,
    bot_events / bot_runs is the right place — not a file."""
    return None


__all__ = ["load_state", "save_state", "default_state"]
