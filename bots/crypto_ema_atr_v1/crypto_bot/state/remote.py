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


_logged_key = False


def _key() -> str:
    """The state row's primary key. Distinct per branch in CI so a feature
    branch run can't clobber main's state. Falls back to 'default'.

    GOTCHA (bit us 2026-07-24). `GITHUB_REF_NAME` is injected automatically
    by GitHub Actions and equals the branch name — so on GHA this resolved
    to 'main'. When the bot moved to Fly that variable no longer exists, the
    chain fell through to 'default', and the bot silently began writing a
    BRAND NEW state row: price history reset to zero, position tracking
    reset, and the accumulated `main` row orphaned. Nothing errored. The
    only visible symptom was the dashboard reporting "warming up regime
    filter (42/55 prices)" for a bot that had been running since March.

    Set STATE_KEY explicitly in any non-GHA environment. On Fly that means
    CRYPTO_STATE_KEY (the agent_runner scheduler strips the CRYPTO_ prefix
    per-job), which is set in fly.toml.

    The resolved key is logged once per process so a mismatch is obvious in
    the logs instead of silently forking state again.
    """
    global _logged_key
    key = os.getenv("GITHUB_REF_NAME") or os.getenv("STATE_KEY") or "default"
    if not _logged_key:
        source = (
            "GITHUB_REF_NAME" if os.getenv("GITHUB_REF_NAME")
            else "STATE_KEY" if os.getenv("STATE_KEY")
            else "fallback"
        )
        print(f"[state.remote] using bot_state key={key!r} (from {source})")
        _logged_key = True
    return key


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
