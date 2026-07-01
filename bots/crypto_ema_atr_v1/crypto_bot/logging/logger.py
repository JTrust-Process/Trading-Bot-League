# crypto_bot/logging/logger.py
#
# Single logging entry point. Writes to:
#   1. stdout (visible in GHA logs)
#   2. Supabase bot_logs (queryable from dashboard)
#
# Fixes from audit:
#   - Issue 14: removed CSV writes (file doesn't persist between GHA runs anyway)
#   - Issue 15: uses shared _supabase helper instead of duplicating client logic

from datetime import datetime, timezone
from crypto_bot.logging._supabase import safe_insert, now_iso


def _log_to_supabase(level: str, message: str, run_id: str | None) -> None:
    safe_insert("bot_logs", {
        "run_id":     run_id,
        "level":      level,
        "message":    message,
        "created_at": now_iso(),
    })


def append_log(message: str, level: str = "INFO", run_id: str | None = None) -> None:
    """The single logging call for the whole bot. Mirrors to stdout + Supabase."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {message}")
    _log_to_supabase(level, message, run_id)


def log(message: str, run_id: str | None = None) -> None:
    append_log(message, level="INFO", run_id=run_id)


def log_warn(message: str, run_id: str | None = None) -> None:
    append_log(message, level="WARN", run_id=run_id)


def log_error(message: str, run_id: str | None = None) -> None:
    append_log(message, level="ERROR", run_id=run_id)