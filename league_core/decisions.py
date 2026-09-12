"""league_core.decisions — the BotDecision contract. DORMANT.

PHASE B. Nothing imports this module. It writes nothing, reads nothing, and
has no side effects. It exists so the contract can be argued with, tested,
and corrected BEFORE anything depends on it.

Do not wire this into a bot. That is Phase C, and it starts with a
research/paper bot, never a live one.

────────────────────────────────────────────────────────────────────────────
WHAT A DECISION IS

    bot_decisions  = what the bot WANTED      (intent, including declined)
    bot_trades     = what the broker DID      (fact)
    bot_positions  = what we HOLD             (state)

The gap between the first two is where every significant bug in this system
has lived. `bot_trades` records fills; a trade that never happened leaves no
row at all. So a stop-loss silently skipped, an order refused by the risk
gate, a candidate declined on a stale signal — all of these were invisible
by construction, and each was found only by reading data by hand weeks later.

A BotDecision is the record of the path not taken. That is its whole value.
Uniformity across bots is a nice secondary effect.

────────────────────────────────────────────────────────────────────────────
THE TRUST BOUNDARY

A bot describes what it wants. It does NOT get to describe its own
authority. Three fields are therefore absent from this dataclass entirely,
so that passing them raises TypeError rather than being silently honoured:

    resolved_mode   Which mode the decision is EVALUATED under. Comes from
                    the bot_registry row, resolved by League Core at
                    execution time. A bot asserting 'paper' while the
                    registry says 'live' — or the reverse — is the entire
                    failure surface. The registry is truth; the bot's
                    `intended_mode` is recorded only so a disagreement
                    between the two becomes queryable instead of invisible.

    created_at      Set by League Core or by a database default. Bot clocks
                    drift and bot environments lie about timezones — this
                    system has already been bitten by a scheduler running a
                    day-of-week it did not believe it was running.

    can_place_orders / any authority flag
                    Registry only, enforced in league_core.risk. A decision
                    cannot grant itself permission.

`bot_id` IS accepted here because a dormant contract has no execution
context to derive it from. When Phase C adds a writer, that writer must
override it from LEAGUE_BOT_ID rather than trusting this value — otherwise
bot A can file decisions as bot B. There is a TODO at to_row() for this.

────────────────────────────────────────────────────────────────────────────
ACTIONABLE vs NON_ACTIONABLE

    ACTIONABLE      BUY, SELL, COVER     may reach an executor
    NON_ACTIONABLE  HOLD, SKIP, ALERT    record-only, structurally inert

HOLD and SKIP are the valuable rows — they are the missed-opportunity
signal generalised to every bot — and they must be incapable of causing a
trade by construction rather than by convention. `is_actionable` is a
derived property with no setter; there is no way to mark a SKIP executable.

One asymmetry that must survive into the executor: decisions that CLOSE a
position must never be gated by throttles that gate ones that OPEN. Daily
trade caps, cooldowns, minimum hold periods and circuit breakers are ENTRY
concerns. Applying them to exits is how this system disabled its own
stop-losses five separate times.

Note carefully that this distinction is `position_effect`, NOT `action`.
SELL is ambiguous — a long exit and a short entry are both SELL — so an
executor keying the exemption on the action would let a short ENTRY bypass
the entry cap. See the POSITION EFFECT section below.

────────────────────────────────────────────────────────────────────────────
IMPORT POLICY

This module imports ONLY the standard library plus league_core.contracts
(which is itself dependency-free and side-effect-free). It must never
import requests, supabase, a broker client, an order path, a notifier, or
any AI/LLM SDK. _decisions_smoke.py enforces this by parsing the AST, and
CI runs it on every push.

The property is "cannot reach a broker", not "does not currently".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# contracts.py is dependency-free and side-effect-free (see its docstring).
# Importing the vocabulary rather than restating it avoids the
# two-copies-kept-in-sync-by-comment pattern that has already produced
# divergent symbol allowlists and a seed file that disagreed with its own
# database row.
from league_core.contracts import BOT_TYPES, MODES


# ── Actions ──────────────────────────────────────────────────────────────────

BUY = "BUY"
SELL = "SELL"
COVER = "COVER"
HOLD = "HOLD"
SKIP = "SKIP"
ALERT = "ALERT"

#: Actions that may be handed to an executor.
ACTIONABLE = frozenset({BUY, SELL, COVER})

#: Actions that are records only. An executor must refuse these outright.
NON_ACTIONABLE = frozenset({HOLD, SKIP, ALERT})

ACTIONS = ACTIONABLE | NON_ACTIONABLE

#: Action-level approximation of "this is an exit", mirroring
#: league_core.risk.CLOSE_ACTIONS.
#:
#: ⚠ DEPRECATED AS A SOURCE OF TRUTH — use `position_effect` instead.
#:
#: Kept because league_core.risk currently keys its daily-trade-cap
#: exemption on exactly this set, and the two must not silently diverge
#: while that remains true. But the set is WRONG in one important case, and
#: that case is why position_effect exists: see POSITION EFFECT below.
CLOSE_ACTIONS = frozenset({SELL, COVER})

#: Action-level approximation of "this is an entry". Same caveat.
OPEN_ACTIONS = frozenset({BUY})


# ── Position effect ──────────────────────────────────────────────────────────
#
# WHY THIS FIELD EXISTS
#
# `action` alone cannot tell an entry from an exit. SELL is ambiguous:
#
#     long  SELL   sell shares you own            -> CLOSE
#     short SELL   sell borrowed shares to open   -> OPEN
#
# Both are SELL. They are opposite in every way that matters to a risk
# gate. This was found by short_watchlist_v1's decision mapper: a short
# ENTRY mapped to SELL and the contract reported `is_close=True`, which is
# exactly backwards — the one moment entry-side throttles most need to
# apply is when a new position is being opened.
#
# So the entry/exit distinction is now carried EXPLICITLY, and `is_open` /
# `is_close` derive from it rather than from the action.
#
# ⚠ CONSEQUENCE FOR RISK — READ BEFORE WIRING AN EXECUTOR
#
# league_core.risk exempts CLOSE_ACTIONS from the daily trade cap, keyed on
# ACTION. Under that rule a short ENTRY (SELL + OPEN) would be treated as an
# exit and bypass the entry cap entirely. No bot emits short entries through
# the risk gate today — short_watchlist_v1 is paper and imports no order
# client — so nothing is currently exposed. But any executor wiring shorts
# to a live path MUST switch risk.py to key on position_effect first.
# Recording that here because the gap is invisible from inside risk.py.

OPEN = "OPEN"
CLOSE = "CLOSE"
NONE = "NONE"

POSITION_EFFECTS = frozenset({OPEN, CLOSE, NONE})

#: What each action means when the caller does not say. SELL defaults to
#: CLOSE for long-only compatibility — every live bot in this system is
#: long-only, so an unannotated SELL is overwhelmingly a long exit. A short
#: entry must therefore be EXPLICIT about position_effect=OPEN, which is
#: the right way round: the unusual, riskier case is the one that has to
#: declare itself.
DEFAULT_POSITION_EFFECT = {
    BUY:   OPEN,
    SELL:  CLOSE,
    COVER: CLOSE,
    HOLD:  NONE,
    SKIP:  NONE,
    ALERT: NONE,
}


# ── Asset classes ────────────────────────────────────────────────────────────
#
# These MUST match the CHECK constraint on bot_signals.asset_class in
# supabase/migrations/010_bot_signals.sql. A contract that accepts a value
# the database rejects is not a contract — it just moves the failure to
# insert time, in production, fail-silently.
#
# Note for anyone reconciling this against an earlier design sketch: that
# sketch listed `fund` and `cash`. The schema has neither. A fund/ETF is
# `etf`; a cash-equivalent position (SGOV and similar) is also `etf`,
# because that is what is actually bought. Adding a genuine `cash` class
# would require a migration altering the CHECK constraint first.
ASSET_CLASSES = frozenset({
    "equity",
    "etf",
    "crypto",
    "bond",
    "option",
    "option_spread",
})


# ── Errors ───────────────────────────────────────────────────────────────────


class DecisionValidationError(ValueError):
    """Raised when a BotDecision is constructed with invalid input.

    Deliberately a ValueError subclass so existing `except ValueError`
    handlers behave sensibly, and deliberately RAISING rather than
    returning a sentinel: this is a construction-time programming error in
    a bot, caught in tests and CI, never at runtime inside a trading loop.

    The Phase C writer is what must be fail-soft. This type is not.
    """


# ── Helpers ──────────────────────────────────────────────────────────────────


def _clean_float(value: Any, field_name: str, *, allow_negative: bool) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise DecisionValidationError(
            f"{field_name} must be a number or None, got {value!r}"
        ) from None
    if math.isnan(f) or math.isinf(f):
        raise DecisionValidationError(
            f"{field_name} must be finite, got {value!r}"
        )
    if not allow_negative and f < 0:
        raise DecisionValidationError(
            f"{field_name} must not be negative, got {f!r}"
        )
    return f


# ── The contract ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BotDecision:
    """One decision a bot reached about one symbol at one moment.

    Frozen on purpose. A decision is a historical record, not mutable
    state. Nothing should read a decision back to drive control flow —
    doing so recreates the stale-state class of bug (a dollar-denominated
    high-water mark surviving across positions; candle counters frozen by a
    capped list) that this system has hit twice.

    Required:
        bot_id, bot_type, asset_class, symbol, action, reason

    Optional:
        confidence, suggested_amount_usd, suggested_quantity,
        strategy_tags, risk_notes, metadata, run_id, intended_mode

    Absent by design — see the module docstring:
        resolved_mode, created_at, and any authority flag.
    """

    # Required
    bot_id: str
    bot_type: str
    asset_class: str
    symbol: str
    action: str
    reason: str

    # Optional
    confidence: Optional[float] = None
    suggested_amount_usd: Optional[float] = None
    suggested_quantity: Optional[float] = None
    strategy_tags: tuple[str, ...] = ()
    risk_notes: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: Optional[str] = None

    #: What the bot BELIEVES it is running as. Advisory only. League Core
    #: resolves the authoritative mode from bot_registry; a mismatch is a
    #: signal worth alerting on, not a value to act upon.
    intended_mode: Optional[str] = None

    #: OPEN / CLOSE / NONE. Whether this decision opens a position, closes
    #: one, or does neither.
    #:
    #: Leave it None and it is resolved from DEFAULT_POSITION_EFFECT, which
    #: is correct for every long-only case. A SHORT ENTRY must set OPEN
    #: explicitly, because SELL defaults to CLOSE.
    #:
    #: ⚠ THIS IS DECISION INTENT, NOT EXECUTION AUTHORITY.
    #:
    #: It describes what the bot means to do. It does not establish that the
    #: position exists, that the bot may trade, that shorting is permitted,
    #: or that capital is available. Before any live order, League Core and
    #: league_core.risk must independently verify:
    #:
    #:      actual holdings         does this position exist to be closed?
    #:      resolved_mode           from bot_registry, never from the bot
    #:      can_place_orders        from bot_registry
    #:      allow_short             a short entry needs explicit permission
    #:      buying power / caps     max_order_usd, exposure, minimum size
    #:
    #: A decision claiming CLOSE on a position that isn't held must be
    #: refused, not executed. position_effect never shortens the risk path
    #: and never exempts a decision from a check — it only makes the
    #: entry/exit distinction legible to the checks that already exist.
    position_effect: Optional[str] = None

    def __post_init__(self) -> None:
        # ── Required strings ──────────────────────────────────────────────
        for name in ("bot_id", "bot_type", "asset_class", "symbol", "action", "reason"):
            val = getattr(self, name)
            if not isinstance(val, str) or not val.strip():
                raise DecisionValidationError(
                    f"{name} is required and must be a non-empty string, got {val!r}"
                )

        # ── Normalise ─────────────────────────────────────────────────────
        # frozen=True blocks plain assignment; object.__setattr__ is the
        # documented escape hatch for __post_init__ normalisation.
        object.__setattr__(self, "bot_id", self.bot_id.strip())
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "action", self.action.strip().upper())
        object.__setattr__(self, "bot_type", self.bot_type.strip().lower())
        object.__setattr__(self, "asset_class", self.asset_class.strip().lower())

        # ── Vocabulary ────────────────────────────────────────────────────
        if self.action not in ACTIONS:
            raise DecisionValidationError(
                f"action must be one of {sorted(ACTIONS)}, got {self.action!r}"
            )
        if self.asset_class not in ASSET_CLASSES:
            raise DecisionValidationError(
                f"asset_class must be one of {sorted(ASSET_CLASSES)}, "
                f"got {self.asset_class!r}"
            )
        if self.bot_type not in BOT_TYPES:
            raise DecisionValidationError(
                f"bot_type must be one of {sorted(BOT_TYPES)}, got {self.bot_type!r}"
            )
        if self.intended_mode is not None:
            mode = str(self.intended_mode).strip().lower()
            if mode not in MODES:
                raise DecisionValidationError(
                    f"intended_mode must be one of {sorted(MODES)} or None, "
                    f"got {self.intended_mode!r}"
                )
            object.__setattr__(self, "intended_mode", mode)

        # ── Position effect ───────────────────────────────────────────────
        # Omitted -> resolved from the action. Supplied -> validated, and a
        # contradiction is an ERROR rather than something quietly corrected.
        # "SKIP, but treat it as opening a position" is not a typo to fix
        # silently; it means the caller has misunderstood the contract, and
        # that is worth surfacing at construction time in a test rather than
        # at execution time in production.
        if self.position_effect is None:
            object.__setattr__(self, "position_effect",
                               DEFAULT_POSITION_EFFECT[self.action])
        else:
            eff = str(self.position_effect).strip().upper()
            if eff not in POSITION_EFFECTS:
                raise DecisionValidationError(
                    f"position_effect must be one of {sorted(POSITION_EFFECTS)} "
                    f"or None, got {self.position_effect!r}"
                )
            if self.action in NON_ACTIONABLE and eff != NONE:
                raise DecisionValidationError(
                    f"action={self.action} is non-actionable and must have "
                    f"position_effect={NONE}, got {eff}. A HOLD/SKIP/ALERT "
                    f"does not open or close anything."
                )
            if self.action in ACTIONABLE and eff == NONE:
                raise DecisionValidationError(
                    f"action={self.action} is actionable and must be "
                    f"{OPEN} or {CLOSE}, got {NONE}. If it neither opens nor "
                    f"closes a position, it is a HOLD."
                )
            if self.action == BUY and eff != OPEN:
                raise DecisionValidationError(
                    f"BUY always opens a position; got position_effect={eff}. "
                    f"To close a short, use {COVER}."
                )
            if self.action == COVER and eff != CLOSE:
                raise DecisionValidationError(
                    f"COVER always closes a short; got position_effect={eff}. "
                    f"To open a short, use {SELL} with position_effect={OPEN}."
                )
            object.__setattr__(self, "position_effect", eff)

        # ── Numerics ──────────────────────────────────────────────────────
        conf = _clean_float(self.confidence, "confidence", allow_negative=True)
        if conf is not None and not (0.0 <= conf <= 1.0):
            raise DecisionValidationError(
                f"confidence must be between 0 and 1 inclusive, got {conf!r}"
            )
        object.__setattr__(self, "confidence", conf)

        # Zero is permitted (a genuine "size nothing" suggestion); negative
        # is not. A negative notional or quantity is how a corrupt row with
        # price -2339.84 and size -0.00427380 reached crypto_trades and
        # silently poisoned every aggregate over that table for months.
        object.__setattr__(self, "suggested_amount_usd", _clean_float(
            self.suggested_amount_usd, "suggested_amount_usd", allow_negative=False))
        object.__setattr__(self, "suggested_quantity", _clean_float(
            self.suggested_quantity, "suggested_quantity", allow_negative=False))

        # ── Collections ───────────────────────────────────────────────────
        # Copied defensively. A caller mutating the list it passed in must
        # not be able to mutate a decision that has already been recorded.
        tags = self.strategy_tags
        if tags is None:
            tags = ()
        if isinstance(tags, str):
            raise DecisionValidationError(
                "strategy_tags must be a sequence of strings, not a bare string"
            )
        try:
            clean_tags = tuple(str(t).strip() for t in tags if str(t).strip())
        except TypeError:
            raise DecisionValidationError(
                f"strategy_tags must be iterable, got {tags!r}"
            ) from None
        object.__setattr__(self, "strategy_tags", clean_tags)

        meta = self.metadata
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise DecisionValidationError(
                f"metadata must be a dict, got {type(meta).__name__}"
            )
        object.__setattr__(self, "metadata", dict(meta))

        if self.risk_notes is not None and not isinstance(self.risk_notes, str):
            raise DecisionValidationError(
                f"risk_notes must be a string or None, got {type(self.risk_notes).__name__}"
            )
        if self.run_id is not None:
            object.__setattr__(self, "run_id", str(self.run_id))

    # ── Derived ───────────────────────────────────────────────────────────

    @property
    def is_actionable(self) -> bool:
        """True only for BUY / SELL / COVER.

        Derived from the action, with no setter and no override. A SKIP
        cannot be marked executable by any caller, which is the point.
        """
        return self.action in ACTIONABLE

    @property
    def is_close(self) -> bool:
        """True when this decision CLOSES a position.

        Derived from position_effect, NOT from the action — a short entry
        is a SELL that opens, and treating it as an exit is precisely the
        bug this field was added to fix.

        An executor must not apply entry-side throttles (daily trade caps,
        cooldowns, minimum hold periods, circuit breakers) when this is
        True. Those are entry concerns; applying them to exits is how this
        system disabled its own stop-losses five separate times.
        """
        return self.position_effect == CLOSE

    @property
    def is_open(self) -> bool:
        """True when this decision OPENS a position.

        Covers BUY and a short SELL alike. Entry-side throttles apply.
        """
        return self.position_effect == OPEN

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_row(self) -> dict[str, Any]:
        """JSON-serialisable dict of what the BOT supplied.

        Deliberately omits `resolved_mode` and `created_at`. Those are
        League Core's to add — the former from bot_registry, the latter
        from a database default. If you find yourself adding them here,
        re-read the trust-boundary section of the module docstring first.

        TODO (Phase C): the writer must override `bot_id` from
        LEAGUE_BOT_ID in the scheduler's per-job env scope rather than
        trusting the value carried here, so one bot cannot file decisions
        under another's identity.

        `strategy_tags` becomes a list because tuples are not JSON types.
        """
        return {
            "bot_id": self.bot_id,
            "bot_type": self.bot_type,
            "asset_class": self.asset_class,
            "symbol": self.symbol,
            "action": self.action,
            "position_effect": self.position_effect,
            "reason": self.reason,
            "confidence": self.confidence,
            "suggested_amount_usd": self.suggested_amount_usd,
            "suggested_quantity": self.suggested_quantity,
            "strategy_tags": list(self.strategy_tags),
            "risk_notes": self.risk_notes,
            "metadata": dict(self.metadata),
            "run_id": self.run_id,
            "intended_mode": self.intended_mode,
            "is_actionable": self.is_actionable,
        }


__all__ = [
    "BUY", "SELL", "COVER", "HOLD", "SKIP", "ALERT",
    "ACTIONS", "ACTIONABLE", "NON_ACTIONABLE",
    "CLOSE_ACTIONS", "OPEN_ACTIONS",
    "OPEN", "CLOSE", "NONE",
    "POSITION_EFFECTS", "DEFAULT_POSITION_EFFECT",
    "ASSET_CLASSES",
    "BotDecision",
    "DecisionValidationError",
]
