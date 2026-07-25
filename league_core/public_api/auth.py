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

def _mask(v: Any) -> str:
    s = str(v or "")
    return s[:4] + "..." + s[-4:] if len(s) > 8 else (s or "?")


def fetch_accounts() -> Optional[list[dict[str, Any]]]:
    """GET the account list. None on any failure (network, non-200, bad
    shape). Distinguishable from [] which would mean 'no accounts'."""
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
    if not isinstance(accounts, list):
        _print(f"account list unexpected shape (keys={list((data or {}).keys())})")
        return None
    return [a for a in accounts if isinstance(a, dict)]


def get_account_id(*, force_refresh: bool = False) -> Optional[str]:
    """Resolve and cache the brokerage accountId for trading calls. None
    on failure.

    Resolution order:
      1. PUBLIC_ACCOUNT_ID env var (preferred — explicit pin), VALIDATED
         against the account list.
      2. GET /userapigateway/trading/account, pick the first accountId.

    VALIDATION (added 2026-07-25). This function previously trusted
    PUBLIC_ACCOUNT_ID blindly. A stale/incorrect pin therefore produced
    `{"code":47050,"message":"Account not found"}` on every order and a
    404 on portfolio v2 — and because etf_rotation_v1 (the only consumer
    of this client) has been live-but-dormant since June, the fault was
    invisible. Its first real rebalance would have failed outright.

    Now:
      * pinned AND present in the account list -> use it.
      * pinned AND NOT in the list             -> refuse (return None) and
                                                  log the valid ids. Trading
                                                  on the wrong account is
                                                  worse than not trading.
      * pinned but the list can't be fetched   -> trust the pin, warn. A
                                                  network blip shouldn't
                                                  halt trading, and Public
                                                  will reject a bad id
                                                  anyway.
      * not pinned                             -> accounts[0], warn when
                                                  there's more than one.

    Set PUBLIC_ACCOUNT_ID_SKIP_VALIDATION=1 to restore the old
    trust-blindly behavior (escape hatch; not recommended).
    """
    if not force_refresh and _account_cache.get("account_id"):
        return str(_account_cache["account_id"])

    pinned = (os.getenv("PUBLIC_ACCOUNT_ID") or "").strip()
    skip_validation = (
        (os.getenv("PUBLIC_ACCOUNT_ID_SKIP_VALIDATION") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    if pinned and skip_validation:
        _print(f"account id {_mask(pinned)} pinned; validation SKIPPED "
               f"(PUBLIC_ACCOUNT_ID_SKIP_VALIDATION set)")
        _account_cache["account_id"] = pinned
        return pinned

    accounts = fetch_accounts()

    if pinned:
        if accounts is None:
            # Couldn't verify. Don't halt trading over a transient failure.
            _print(f"account list unavailable; trusting pinned id {_mask(pinned)}")
            _account_cache["account_id"] = pinned
            return pinned
        valid = {str(a.get("accountId")) for a in accounts if a.get("accountId")}
        if pinned in valid:
            _account_cache["account_id"] = pinned
            return pinned
        _print(
            f"REFUSING: PUBLIC_ACCOUNT_ID={_mask(pinned)} is not in this "
            f"key's account list. Valid ids: "
            f"{sorted(_mask(v) for v in valid) or '(none)'}. "
            f"Fix the secret — orders would fail with 'Account not found'."
        )
        return None

    # Not pinned — fall back to the first account.
    if not accounts:
        _print("no accounts available and PUBLIC_ACCOUNT_ID not set")
        return None
    if len(accounts) > 1:
        _print(
            f"WARNING: {len(accounts)} accounts returned and PUBLIC_ACCOUNT_ID "
            f"is not set; defaulting to the first. Pin it explicitly to avoid "
            f"trading on the wrong account."
        )
    acc_id = accounts[0].get("accountId")
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
    "fetch_accounts",
    "get_account_id",
    "reset_caches",
]
