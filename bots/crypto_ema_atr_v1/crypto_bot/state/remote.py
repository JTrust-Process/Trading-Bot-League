# crypto_bot/state/remote.py
#
# Audit H1 — Supabase fallback for state.json.
#
# GitHub Actions cache is best-effort: caches not accessed for 7 days are
# evicted, and there is no SLA. If state.json disappears between runs, the
# bot defaults to a fresh state with no positions, no cooldowns, and no loss
# streaks — which can lead to duplicate buys, ignored cooldowns, or the
# circuit breaker resetting silently.
#
# This module mirrors state to a single Supabase row keyed by the GitHub
# branch name (or "default" locally). The bot writes to Supabase after every
# successful local save, and reads from Supabase only when the local file is
# missing or unreadable (cache miss or corruption).
#
# Required Supabase table:
#   bot_state (
#     key TEXT PRIMARY KEY,
#     state JSONB NOT NULL,
#     updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
#   )
#
# See supabase/migrations/003_bot_state.sql.

import os

from crypto_bot.logging._supabase import get_client, now_iso

_TABLE = "bot_state"


def _key() -> str:
    """The state row's primary key. Distinct per branch in CI so a feature
    branch run can't clobber main's state. Locally, falls back to 'default'."""
    return os.getenv("GITHUB_REF_NAME") or os.getenv("STATE_KEY") or "default"


def save_state_to_supabase(state: dict) -> None:
    """Best-effort upsert. Never raises — state.json is the source of truth
    on the happy path; this is just a recovery net."""
    try:
        get_client().table(_TABLE).upsert(
            {
                "key":        _key(),
                "state":      state,
                "updated_at": now_iso(),
            },
            on_conflict="key",
        ).execute()
    except Exception as e:
        print(f"[state.remote] save_state_to_supabase failed: {e}")


def load_state_from_supabase() -> dict | None:
    """Return the most-recent saved state for this key, or None if missing
    / on error. Caller decides whether to use it (typically only when local
    state.json is absent)."""
    try:
        resp = (
            get_client()
            .table(_TABLE)
            .select("state")
            .eq("key", _key())
            .limit(1)
            .execute()
        )
        rows = resp.data
        if rows and isinstance(rows, list) and isinstance(rows[0], dict):
            state = rows[0].get("state")
            if isinstance(state, dict):
                return state
    except Exception as e:
        print(f"[state.remote] load_state_from_supabase failed: {e}")
    return None
