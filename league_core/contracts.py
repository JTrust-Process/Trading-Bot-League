"""league_core.contracts — the bot interface standard.

Every bot in the League satisfies two things:

  1. A row in the `bot_registry` Supabase table (declarative — see
     supabase/migrations/001_bot_registry.sql and the seed file).
  2. The runtime contract below: 5 lifecycle calls + 3 logging calls.

We use a Protocol (structural typing) rather than a base class so existing
bots keep their current Monitor classes — they just need to expose the same
method names. New bots can compose helpers from `league_core.status` and
later modules without inheriting anything.

This file has zero side effects and zero runtime dependencies. Importing it
is safe in any context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


# ── Allowed values (mirror the CHECK constraints in the SQL schema) ───────────

BOT_TYPES = (
    "stock",
    "crypto",
    "etf",
    "bond",
    "short",
    "options",
    "multi_leg_options",
    "agent_research",
)

MODES = ("research", "paper", "live")

REGISTRY_STATUSES = ("enabled", "disabled", "killed")

RUN_STATUSES = ("running", "success", "warning", "failed", "timeout")

HEALTH_STATES = ("healthy", "degraded", "down", "unknown", "muted")


# ── Bot configuration (in-process mirror of a bot_registry row) ───────────────


@dataclass
class BotConfig:
    """In-process snapshot of a bot's bot_registry row.

    Bots construct one of these at startup (typically by reading a small
    config file or env vars; later we'll fetch the canonical row from
    Supabase). The League helpers use this to attribute every write to the
    correct bot_id and to enforce capability gates locally as defense in
    depth alongside the Supabase-side checks.
    """

    bot_id: str
    bot_name: str
    bot_type: str                       # one of BOT_TYPES
    mode: str                           # one of MODES
    allowed_instruments: list[str] = field(default_factory=list)
    can_place_orders: bool = False
    manual_approval_required: bool = True
    max_order_usd: float = 0.0
    max_daily_loss_usd: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_daily_trades: int = 0           # 0 means "no explicit cap"
    max_open_positions: Optional[int] = None
    max_exposure_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.bot_type not in BOT_TYPES:
            raise ValueError(
                f"BotConfig.bot_type must be one of {BOT_TYPES!r}, got {self.bot_type!r}"
            )
        if self.mode not in MODES:
            raise ValueError(
                f"BotConfig.mode must be one of {MODES!r}, got {self.mode!r}"
            )
        # Hard rule from the safety policy — agent bots can never place orders.
        # This is checked in three places (here, in the Supabase row, and in
        # the risk preflight) precisely because the consequence of getting it
        # wrong is unacceptable.
        if self.bot_type == "agent_research" and self.can_place_orders:
            raise ValueError(
                "agent_research bots must have can_place_orders=False"
            )

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_research(self) -> bool:
        return self.mode == "research"


# ── The runtime contract ─────────────────────────────────────────────────────


class LeagueBot(Protocol):
    """Structural type a bot satisfies to participate in the League.

    Existing Stock and Crypto Monitor classes already provide most of these
    method names. The Step 1b adapter wires their existing calls to the
    League helpers without changing any trading logic.
    """

    config: BotConfig

    # Lifecycle
    def start_run(self, trigger: str = "cron") -> str: ...           # returns run_id
    def end_run(self, run_id: str, status: str = "success") -> None: ...
    def heartbeat(
        self, health: str = "healthy", details: Optional[dict] = None
    ) -> None: ...

    # Outputs (Step 1a only ships heartbeat/run lifecycle; the rest land in
    # later stages and are part of the Protocol so the contract is stable.)
    def log_signal(self, run_id: str, **fields: object) -> None: ...
    def log_trade(self, run_id: str, **fields: object) -> None: ...
    def log_error(
        self,
        run_id: str,
        stage: str,
        error: BaseException,
        symbol: Optional[str] = None,
        severity: str = "warning",
    ) -> None: ...
