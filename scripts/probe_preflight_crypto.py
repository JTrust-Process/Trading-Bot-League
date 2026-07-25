"""scripts/probe_preflight_crypto.py — does Public's preflight support CRYPTO?

READ-ONLY PROBE. Places NO orders. The preflight endpoint validates and
prices a hypothetical order; it never submits one.

Why this exists
---------------
We added preflight-based pre-trade safety to the equity order paths
(league_core/public_api/equities.py and bots/stock_momentum_v1/bot.py) on
2026-07-25. We want the same for crypto_ema_atr_v1, but Public's docs
only describe preflight in equity/options terms — `instrument.type` of
CRYPTO is undocumented for that endpoint.

Rather than guess (and risk breaking a live-capital order path), this
script asks the API directly.

Usage
-----
From the repo root, with PUBLIC_SECRET and PUBLIC_ACCOUNT_ID set:

    python -m scripts.probe_preflight_crypto

It runs three probes and prints a verdict:

  1. EQUITY  preflight (SPY, $5)   — control. Should succeed. If this
                                     fails, the problem is auth/account,
                                     not crypto support.
  2. CRYPTO  preflight (BTC, $5)   — the actual question.
  3. Portfolio v2 buyingPower      — shows whether cashOnlyBuyingPower is
                                     even present on this account, which
                                     determines whether the cash-only
                                     check is meaningful.

$5 is used because Public's documented minimums are $5 notional for
fractional equities and $1 for crypto. Nothing is submitted either way.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from league_core.public_api import auth


PREFLIGHT_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/preflight/single-leg"
)
PORTFOLIO_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
)


def _probe(account_id: str, headers: dict, symbol: str, inst_type: str,
           amount: str) -> None:
    label = f"{inst_type} preflight ({symbol}, ${amount})"
    payload = {
        "instrument": {"symbol": symbol, "type": inst_type},
        "orderSide": "BUY",
        "orderType": "MARKET",
        "expiration": {"timeInForce": "DAY"},
        "amount": amount,
    }
    print(f"\n--- {label} ---")
    print(f"request: {json.dumps(payload)}")
    try:
        resp = requests.post(
            PREFLIGHT_URL_TMPL.format(account_id=account_id),
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        print(f"RESULT: network error -> {e!r}")
        return

    print(f"status: {resp.status_code}")
    body_text = (resp.text or "")[:1200]
    print(f"body: {body_text}")

    if resp.status_code == 200:
        print(f"VERDICT: {inst_type} IS supported by preflight.")
        try:
            body = resp.json()
        except ValueError:
            return
        for k in ("orderValue", "estimatedCost", "buyingPowerRequirement",
                  "estimatedCommission", "marginImpact", "marginRequirement"):
            if k in body:
                print(f"  {k}: {body[k]}")
    elif resp.status_code in (400, 422):
        print(f"VERDICT: {inst_type} REJECTED (status {resp.status_code}). "
              f"Read the body above — it distinguishes 'type not supported' "
              f"from an ordinary validation complaint (e.g. below minimum).")
    else:
        print(f"VERDICT: inconclusive (status {resp.status_code}).")


ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"


def _mask(v: str) -> str:
    v = str(v or "")
    return v[:4] + "..." + v[-4:] if len(v) > 8 else (v or "?")


def _list_accounts(headers: dict) -> None:
    """Show what account IDs Public actually reports for this key, and
    compare against whatever PUBLIC_ACCOUNT_ID is pinned to.

    Added 2026-07-25 after both preflight probes returned
    {"code":47050,"message":"Account not found"} and portfolio v2 404'd —
    symptoms of a wrong/stale pinned account id rather than an
    unsupported instrument type.
    """
    print("\n--- accounts reported by Public ---")
    try:
        resp = requests.get(ACCOUNT_URL, headers=headers, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"accounts request failed: {e!r}")
        return
    print(f"status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"body: {resp.text[:600]}")
        return
    try:
        data = resp.json()
    except ValueError:
        print(f"non-JSON body: {resp.text[:400]}")
        return

    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list) or not accounts:
        print(f"unexpected shape, keys={list((data or {}).keys())}")
        print(f"raw: {json.dumps(data)[:600]}")
        return

    print(f"{len(accounts)} account(s):")
    for a in accounts:
        if not isinstance(a, dict):
            continue
        print(f"  accountId={_mask(a.get('accountId'))}  "
              f"full={a.get('accountId')}  "
              f"type={a.get('accountType')}  "
              f"number={a.get('accountNumber')}  "
              f"status={a.get('status')}")

    pinned = (os.getenv("PUBLIC_ACCOUNT_ID") or "").strip()
    valid_ids = {str(a.get("accountId")) for a in accounts if isinstance(a, dict)}
    print(f"\nPUBLIC_ACCOUNT_ID pinned to: {pinned or '(unset)'}")
    if not pinned:
        print("VERDICT: unset — league_core.auth would fall back to accounts[0].")
    elif pinned in valid_ids:
        print("VERDICT: pinned id IS valid. The 400/404 came from something else.")
    else:
        print("VERDICT: *** PINNED ID IS NOT IN THE ACCOUNT LIST ***")
        print("         This is why preflight returns 'Account not found' and")
        print("         portfolio v2 404s. etf_rotation_v1 uses this resolver")
        print("         and is in mode='live' — its next real order would fail.")
        print("         Fix: fly secrets set PUBLIC_ACCOUNT_ID='<one of the ids above>'")


def main() -> int:
    if not os.getenv("PUBLIC_SECRET"):
        print("PUBLIC_SECRET not set — cannot probe.")
        return 1

    headers = auth.auth_headers()
    if headers is None:
        print("Could not obtain access token — check PUBLIC_SECRET.")
        return 1

    # Diagnose account resolution FIRST — a bad account id makes every
    # downstream probe meaningless.
    _list_accounts(headers)

    account_id = auth.get_account_id()
    if not account_id:
        print("Could not resolve account id. Set PUBLIC_ACCOUNT_ID.")
        return 1

    print(f"\nresolver returned: {_mask(account_id)}")
    print("NOTE: preflight places NO orders. This is read-only.")

    # 1. Control — equity.
    _probe(account_id, headers, "SPY", "EQUITY", "5.00")
    # 2. The actual question — crypto.
    _probe(account_id, headers, "BTC", "CRYPTO", "5.00")

    # 3. Buying power shape.
    print("\n--- portfolio v2 buyingPower ---")
    try:
        resp = requests.get(
            PORTFOLIO_URL_TMPL.format(account_id=account_id),
            headers=headers, timeout=20,
        )
        if resp.status_code == 200:
            bp = (resp.json() or {}).get("buyingPower") or {}
            print(json.dumps(bp, indent=2))
            if "cashOnlyBuyingPower" in bp:
                print("VERDICT: cashOnlyBuyingPower present — the cash-only "
                      "check is meaningful on this account.")
            else:
                print("VERDICT: cashOnlyBuyingPower ABSENT — the cash-only "
                      "comparison will be skipped (treated as 'unknown'). "
                      "The margin-impact checks still apply.")
        else:
            print(f"status {resp.status_code}: {resp.text[:400]}")
    except Exception as e:  # noqa: BLE001
        print(f"portfolio probe failed: {e!r}")

    print("\nDone. Paste this output back to decide the crypto approach.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
