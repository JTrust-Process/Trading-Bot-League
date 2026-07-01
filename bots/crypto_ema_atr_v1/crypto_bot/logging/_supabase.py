# crypto_bot/logging/_supabase.py
#
# Shared Supabase client + insert helper.
# Consolidates duplicated logic that was in monitor.py and logger.py (issue 15).

from datetime import datetime, timezone
from crypto_bot.config.settings import get_supabase_url, get_supabase_key

_client = None


def get_client():
    """Lazy singleton — credentials read after load_dotenv() runs."""
    global _client
    if _client is None:
        from supabase import create_client
        _client = create_client(get_supabase_url(), get_supabase_key())
    return _client


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_insert(table: str, data: dict) -> list | None:
    """
    Insert a row — swallows all exceptions so logging never kills the bot.
    Returns the inserted rows on success, None on failure.
    """
    try:
        resp = get_client().table(table).insert(data).execute()
        return resp.data
    except Exception as e:
        print(f"[supabase] Insert failed ({table}): {e}")
        return None