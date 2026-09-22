"""league_core.public_api.payloads — DORMANT Public order payload builders.

PHASE 2. NOTHING IMPORTS THIS MODULE. It sends nothing, reads nothing, and
has no side effects. It is a modeling layer for Public API fields we do not
yet use, so their shape can be argued with and tested before any of them
reaches an order path.

⚠ DO NOT WIRE THIS INTO stock_momentum_v1, crypto_ema_atr_v1, OR
  league_core.public_api.equities. Those three build their own bodies
  today, and league_core/_payload_contract_smoke.py pins exactly what they
  send. Switching a live order path to a different builder is a change to
  order construction — it belongs in its own commit, with the contract test
  updated in the same diff, and only after the field in question has been
  verified against Public's current documented schema.

────────────────────────────────────────────────────────────────────────────
WHY A SEPARATE MODULE RATHER THAN EXTENDING equities._build_payload

Because extending the live builder to "support" a field nobody sends yet
means the live order path grows optional branches that are never exercised
in production but are one default-argument change away from being. This
system has been bitten five times by plausible-looking additions near an
order path. A dormant module cannot do that: it has no callers.

────────────────────────────────────────────────────────────────────────────
FIDELITY TO THE CURRENT LIVE SHAPE

These builders reproduce today's bodies EXACTLY, including two quirks that
are not obviously intentional. They are mirrored rather than "fixed",
because the point of this module is to model what we send, and silently
diverging from the live shape would make any future comparison meaningless:

  1. EQUITY uppercases the symbol; CRYPTO does not.
     equities._build_payload does `symbol.upper()`; crypto's
     place_order_* passes `symbol` through untouched. Both are fed
     uppercase today, so nothing is broken — but they disagree.

  2. Amount/quantity formatting differs.
     EQUITY: f"{round(x, 2):.2f}"  -> "10.00"   (fixed 2dp string)
             f"{float(q):.8f}"     -> "1.50000000"
     CRYPTO: str(round(x, 2))      -> "10.0"    (Python repr, NOT padded)
             str(round(q, 8))      -> "0.00012345"

     So a $10 crypto order sends "10.0" while a $10 equity order sends
     "10.00". Public evidently accepts both. Mirrored faithfully.

────────────────────────────────────────────────────────────────────────────
FIELD POLICY

BLOCKED_FIELDS are ones this module will never emit. Each corresponds to a
capability that is not safe to enable given where this system currently
stands — see the per-field notes on the constant below.

MODELLED_FIELDS are ones these builders CAN emit, but only when explicitly
asked. Nothing defaults on. `useMargin` is the only one with a near-term
case for going live.
"""

from __future__ import annotations

from typing import Any, Optional


# ── Vocabulary ───────────────────────────────────────────────────────────────

EQUITY = "EQUITY"
CRYPTO = "CRYPTO"

BUY = "BUY"
SELL = "SELL"
SIDES = frozenset({BUY, SELL})

OPEN = "OPEN"
CLOSE = "CLOSE"
OPEN_CLOSE_VALUES = frozenset({OPEN, CLOSE})


#: The exact key set every live order body uses today, minus the
#: amount/quantity leg. _payload_contract_smoke pins this against the real
#: builders; this constant lets the dormant builders be compared to it.
LIVE_BASE_KEYS = frozenset({
    "orderId", "instrument", "orderSide", "orderType", "expiration",
})


#: Fields this module will NEVER emit, with the reason each is refused.
#: These are not "not implemented yet" — they are deliberate refusals.
BLOCKED_FIELDS: dict[str, str] = {
    "orderClass":
        "Bracket orders put the stop on Public's side. Our stops are "
        "client-side and were unenforced in five distinct ways until "
        "2026-08. Two stop systems that can disagree is worse than one "
        "that works. Prove the client side over 30 days first.",
    "takeProfit":
        "Bracket take-profit leg. Blocked because our take-profit is "
        "DYNAMIC: it is rescaled every cycle from trend strength "
        "(take_profit_base scaled by trend_strong_threshold) and is "
        "skipped entirely when bar data or ATR is unavailable. A "
        "server-side leg fixed at entry cannot track that, so the two "
        "would silently disagree about the exit price. Bracket support "
        "needs its own validation and design pass, it does not fit the "
        "tiny notional/fractional flow we actually place (orders of "
        "$5-$25, often fractional), and it must never appear in these "
        "dormant MARKET-order builders until explicitly supported.",
    "stopLoss":
        "Bracket stop-loss leg. Blocked for the same reason as "
        "takeProfit and one worse: our stop is regime-adjusted "
        "(tightened to 0.8x in a bear regime) and recomputed each cycle, "
        "so a server-side stop placed at entry would drift out of sync "
        "with the client-side one. Two stop systems that can disagree is "
        "strictly worse than one that works, and ours only started "
        "working reliably in 2026-08 after five separate defects that "
        "each disabled it. Prove the client side over 30 days of live "
        "trades before moving any part of it to Public. Requires "
        "separate validation and design; incompatible with the current "
        "tiny notional/fractional live flow; must not appear in these "
        "dormant MARKET-order builders until explicitly supported.",
    "bracketId":
        "Bracket correlation id, grouping the entry with its exit legs. "
        "Blocked because NOTHING in our data model represents an order "
        "GROUP: bot_trades and bot_positions are single-leg, and "
        "risk.preflight validates against the preflight/single-leg "
        "endpoint only. Emitting a bracketId would create broker-side "
        "state with no local counterpart, which is precisely how "
        "positions get orphaned. Needs its own validation and schema "
        "design first, does not fit the tiny notional/fractional flow, "
        "and must not appear in these dormant MARKET-order builders "
        "until explicitly supported.",
    "equityMarketSession":
        "24/5 trading. Every order we place is MARKET, and a market order "
        "into a thin overnight book fills badly. This needs LIMIT orders "
        "first, not a session flag.",
    "taxLotMatchingInstructions":
        "Tax lot selling. Read-only lot viewing should come first; "
        "choosing lots changes realised PnL attribution and nothing in "
        "our accounting models lots yet.",
}


#: Fields these builders can emit on explicit request. None default on.
MODELLED_FIELDS = frozenset({"useMargin", "openCloseIndicator"})


class PayloadPolicyError(ValueError):
    """Raised when a payload would violate this module's field policy.

    A ValueError subclass so ordinary handling works, and deliberately
    RAISING: these are construction-time programming errors caught by
    tests, never runtime conditions inside a trading loop.
    """


# ── Internals ────────────────────────────────────────────────────────────────


def _require_one_leg(amount_usd: Optional[float], quantity: Optional[float]) -> None:
    if (amount_usd is None) == (quantity is None):
        raise PayloadPolicyError(
            "exactly one of amount_usd / quantity must be provided "
            f"(got amount_usd={amount_usd!r}, quantity={quantity!r})"
        )


def _validate_common(order_id: str, side: str, symbol: str) -> None:
    if not isinstance(order_id, str) or not order_id.strip():
        raise PayloadPolicyError(f"order_id must be a non-empty string, got {order_id!r}")
    if side not in SIDES:
        raise PayloadPolicyError(f"side must be one of {sorted(SIDES)}, got {side!r}")
    if not isinstance(symbol, str) or not symbol.strip():
        raise PayloadPolicyError(f"symbol must be a non-empty string, got {symbol!r}")


def _positive(value: float, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise PayloadPolicyError(f"{name} must be numeric, got {value!r}") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise PayloadPolicyError(f"{name} must be finite, got {value!r}")
    if f <= 0:
        raise PayloadPolicyError(f"{name} must be > 0, got {f!r}")
    return f


# ── Equity ───────────────────────────────────────────────────────────────────


def build_equity_market_order_payload(
    order_id: str,
    side: str,
    symbol: str,
    *,
    amount_usd: Optional[float] = None,
    quantity: Optional[float] = None,
    use_margin: Optional[bool] = None,
    open_close: Optional[str] = None,
) -> dict[str, Any]:
    """Build an equity MARKET / DAY order body. Pure. Sends nothing.

    With `use_margin=None` and `open_close=None` — the defaults — the result
    is byte-identical to what league_core.public_api.equities._build_payload
    produces today. That equivalence is asserted in the smoke test and is
    the whole reason this builder is shaped the way it is.

    use_margin:
        None   omit the field entirely — CURRENT LIVE BEHAVIOUR.
        False  emit "useMargin": false.
        True   REFUSED. See below.

        Why False is interesting: the existing cash-only guard compares
        buyingPowerRequirement against cashOnlyBuyingPower from portfolio
        v2, and it FAILS OPEN when that fetch returns None — a transient
        500 silently removes the only protection. A field on the request
        cannot fail open that way.

        Why True is refused: this account is cash-only
        (cashOnlyBuyingPower == buyingPower == optionsBuyingPower, probed
        2026-07-25). Asking for margin is never the intent here, and a
        builder that can express it is a builder that can be called with
        the wrong literal. Relax this deliberately if that ever changes.

        NOTE before going live with False: equities.py:44-53 records that
        useMargin was announced 2026-06-16 but was NOT in the documented
        request schema as of 2026-07-24. Verify against current docs first
        — an unknown field may be ignored, or may be rejected.

    open_close:
        None    omit — CURRENT LIVE BEHAVIOUR.
        'OPEN'  / 'CLOSE'  emit "openCloseIndicator".

        Maps 1:1 onto BotDecision.position_effect. A long entry is
        BUY+OPEN, a long exit SELL+CLOSE, a SHORT ENTRY is SELL+OPEN, and
        covering is BUY+CLOSE. Note the last one: our BotDecision action
        COVER has no Public equivalent — it translates to orderSide=BUY
        with openCloseIndicator=CLOSE.
    """
    _validate_common(order_id, side, symbol)
    _require_one_leg(amount_usd, quantity)

    body: dict[str, Any] = {
        "orderId":    order_id,
        "instrument": {"symbol": symbol.upper(), "type": EQUITY},
        "orderSide":  side,
        "orderType":  "MARKET",
        "expiration": {"timeInForce": "DAY"},
    }
    if amount_usd is not None:
        body["amount"] = f"{round(_positive(amount_usd, 'amount_usd'), 2):.2f}"
    if quantity is not None:
        body["quantity"] = f"{_positive(quantity, 'quantity'):.8f}"

    if use_margin is not None:
        if use_margin is True:
            raise PayloadPolicyError(
                "use_margin=True is refused: this is a cash account and "
                "margin is never the intent. Pass False to force cash-only, "
                "or None to omit the field (current live behaviour)."
            )
        if use_margin is not False:
            raise PayloadPolicyError(
                f"use_margin must be None, True or False, got {use_margin!r}"
            )
        body["useMargin"] = False

    if open_close is not None:
        oc = str(open_close).strip().upper()
        if oc not in OPEN_CLOSE_VALUES:
            raise PayloadPolicyError(
                f"open_close must be one of {sorted(OPEN_CLOSE_VALUES)} or "
                f"None, got {open_close!r}"
            )
        body["openCloseIndicator"] = oc

    return body


# ── Crypto ───────────────────────────────────────────────────────────────────


def build_crypto_market_order_payload(
    order_id: str,
    side: str,
    symbol: str,
    *,
    amount_usd: Optional[float] = None,
    quantity: Optional[float] = None,
) -> dict[str, Any]:
    """Build a crypto MARKET / DAY order body. Pure. Sends nothing.

    Reproduces crypto_bot/exchange/public_api.py exactly, including the two
    divergences from the equity builder documented in the module docstring:
    the symbol is NOT uppercased, and amounts use str(round(x, n)) rather
    than a fixed-width format.

    There is deliberately NO use_margin parameter. Margin is an equities
    concept; a crypto order carrying useMargin would be meaningless at
    best. The smoke test asserts the field can never appear here, so this
    stays true if someone later copies the equity builder.

    No session, open/close, bracket or tax-lot parameters either — see
    BLOCKED_FIELDS.
    """
    _validate_common(order_id, side, symbol)
    _require_one_leg(amount_usd, quantity)

    body: dict[str, Any] = {
        "orderId":    order_id,
        "instrument": {"symbol": symbol, "type": CRYPTO},
        "orderSide":  side,
        "orderType":  "MARKET",
        "expiration": {"timeInForce": "DAY"},
    }
    if amount_usd is not None:
        body["amount"] = str(round(_positive(amount_usd, "amount_usd"), 2))
    if quantity is not None:
        body["quantity"] = str(round(_positive(quantity, "quantity"), 8))
    return body


# ── Guards ───────────────────────────────────────────────────────────────────


def assert_no_blocked_fields(payload: dict[str, Any]) -> None:
    """Raise if a payload carries any BLOCKED_FIELDS key.

    Intended as a last-line check for a future writer: whatever built the
    body, it must not contain a capability we have decided not to enable.
    Cheap enough to call on every order.
    """
    if not isinstance(payload, dict):
        raise PayloadPolicyError(f"payload must be a dict, got {type(payload).__name__}")
    present = sorted(k for k in BLOCKED_FIELDS if k in payload)
    if present:
        reasons = "; ".join(f"{k}: {BLOCKED_FIELDS[k]}" for k in present)
        raise PayloadPolicyError(
            f"payload carries blocked field(s) {present}. {reasons}"
        )


def assert_matches_live_shape(payload: dict[str, Any]) -> None:
    """Raise unless `payload` has exactly today's live key set.

    Base keys plus exactly one of amount/quantity, and nothing else. Use
    this to prove a candidate body is identical to what we already send,
    which is the precondition for swapping a builder without changing
    behaviour.
    """
    if not isinstance(payload, dict):
        raise PayloadPolicyError(f"payload must be a dict, got {type(payload).__name__}")
    keys = set(payload)
    legs = keys & {"amount", "quantity"}
    if len(legs) != 1:
        raise PayloadPolicyError(
            f"expected exactly one of amount/quantity, got {sorted(legs)}"
        )
    expected = set(LIVE_BASE_KEYS) | legs
    if keys != expected:
        raise PayloadPolicyError(
            f"payload does not match the live shape; diff={sorted(keys ^ expected)}"
        )


__all__ = [
    "EQUITY", "CRYPTO", "BUY", "SELL", "SIDES",
    "OPEN", "CLOSE", "OPEN_CLOSE_VALUES",
    "LIVE_BASE_KEYS", "BLOCKED_FIELDS", "MODELLED_FIELDS",
    "PayloadPolicyError",
    "build_equity_market_order_payload",
    "build_crypto_market_order_payload",
    "assert_no_blocked_fields",
    "assert_matches_live_shape",
]
