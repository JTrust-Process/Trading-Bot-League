"""league_core.public_api.auth — Public.com token exchange + account resolution.

Single source of truth for two pieces of state every Public-touching League
bot needs:

  * Access token. Exchanged from `PUBLIC_SECRET` via Public's auth service.
    Cached in-process; refreshed before expiry.
  * Account ID. Resolved from `PUBLIC_ACCOUNT_ID` (preferred — explicit
    pin) or by fetching the accounts list and picking the first one. Cached
    in-process.

Design notes:
  * Same auth flow that the live stock bot already uses successfully against
    Public's production API (see `Trading Bot/Trading Bot Project/bot.py`,
    class PublicClient.get_account_id / auth headers). We deliberately
    match the wire format so this client is compatible with what Public
    already accepts from your other bots.
  * Module-level caches. agent_runner is single-process/serial so a module
    cache is fine. NOT thread-safe — if a future scheduler runs jobs
    concurrently, replace these with a per-call cache or a lock.
  * Lazy env reads (call time, not import time) — same pattern as the rest
    of league_core.
  * NEVER raises. Returns None on any failure. Callers must check.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


# ── Endpoints ───────────────────────────────────────────────────────────────

AUTH_URL    = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"

DEFAULT_TIMEOUT = 15.0


# ── In-process caches ──────────────────────────────────────────────────────

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}
_account_cache: dict[str, Any] = {"account_id": None}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(f"[public_auth] {msg}", flush=True)


def _redacted(resp) -> str:
    try:
        body = (resp.text or "")[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001
        body = "<unreadable>"
    return f"status={resp.status_code} body[:200]={body!r}"


# ── Token ───────────────────────────────────────────────────────────────────

def get_access_token(
    *,
    secret: Optional[str] = None,
    validity_minutes: int = 60,
    force_refresh: bool = False,
) -> Optional[str]:
    """Fetch (and cache) an access token. None on failure.

    Reads PUBLIC_SECRET from env unless `secret` is passed explicitly. The
    cached token is reused while it's still valid (with a 2-minute safety
    margin) unless `force_refresh=True`.

    Caller pattern: get_access_token() once at the top of a placement,
    pass the result into auth_headers(), proceed. On 401, call again with
    force_refresh=True and retry once — see equities.place_* for the
    canonical retry-once pattern.
    """
    if requests is None:
        _print("requests not installed; cannot authenticate")
        return None

    if secret is None:
        secret = os.getenv("PUBLIC_SECRET", "")
    if not secret:
        _print("PUBLIC_SECRET not set; cannot authenticate")
        return None

    now = time.time()
    if (not force_refresh
            and _token_cache.get("token")
            and now < float(_token_cache.get("expires_at") or 0.0)):
        return str(_token_cache["token"])

    try:
        resp = requests.post(
            AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={"secret": secret, "validityInMinutes": int(validity_minutes)},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        _print(f"auth request failed: {e}")
        return None

    if resp.status_code != 200:
        _print(f"auth failed {_redacted(resp)}")
        return None

    try:
        data = resp.json()
    except ValueError:
        _print(f"auth returned non-JSON {_redacted(resp)}")
        return None

    token = data.get("accessToken")
    if not token:
        _print(f"auth missing accessToken (keys={list(data.keys())})")
        return None

    _token_cache["token"] = token
    # Subtract a 2-minute safety margin so we refresh BEFORE Public expires.
    _token_cache["expires_at"] = now + max(60.0, (validity_minutes - 2) * 60.0)
    return str(token)


def auth_headers(*, force_refresh: bool = False) -> Optional[dict[str, str]]:
    """Convenience: returns headers ready for any authenticated request.
    None if a token can't be obtained."""
    token = get_access_token(force_refresh=force_refresh)
    if token is None:
        return None
    return {"Authorization": f"Bearer {token}"}


# ── Account ID ──────────────────────────────────────────────────────────────

def get_account_id(*, force_refresh: bool = False) -> Optional[str]:
    """Resolve and cache the brokerage accountId for trading calls. None
    on failure.

    Resolution order:
      1. PUBLIC_ACCOUNT_ID env var (preferred — explicit pin).
      2. GET /userapigateway/trading/account, pick the first accountId.

    Pinning via PUBLIC_ACCOUNT_ID is strongly recommended for production
    so the bot can never trade on the wrong account if Public ever returns
    accounts in a different order.
    """
    if not force_refresh and _account_cache.get("account_id"):
        return str(_account_cache["account_id"])

    # 1. Env-pinned wins.
    pinned = (os.getenv("PUBLIC_ACCOUNT_ID") or "").strip()
    if pinned:
        _account_cache["account_id"] = pinned
        return pinned

    # 2. Fall back to the API. Less safe — we'd rather fail than guess wrong.
    if requests is None:
        return None
    headers = auth_headers()
    if headers is None:
        return None
    try:
        resp = requests.get(ACCOUNT_URL, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        _print(f"account list request failed: {e}")
        return None
    if resp.status_code != 200:
        _print(f"account list failed {_redacted(resp)}")
        return None
    try:
        data = resp.json()
    except ValueError:
        _print(f"account list returned non-JSON {_redacted(resp)}")
        return None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list) or not accounts:
        _print(f"account list empty / unexpected shape (keys={list((data or {}).keys())})")
        return None
    first = accounts[0]
    acc_id = first.get("accountId") if isinstance(first, dict) else None
    if not acc_id:
        _print("first account has no accountId field")
        return None
    _account_cache["account_id"] = str(acc_id)
    return str(acc_id)


def reset_caches() -> None:
    """Clear token + account_id caches. Useful for tests; not normally
    called in production."""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0
    _account_cache["account_id"] = None


__all__ = [
    "AUTH_URL",
    "ACCOUNT_URL",
    "get_access_token",
    "auth_headers",
    "get_account_id",
    "reset_caches",
]
