"""league_core — shared helpers for the Trading Bot League.

This package is intentionally tiny in Step 1a:

  contracts   — BotConfig dataclass + LeagueBot Protocol (the bot interface)
  status      — heartbeat / start_run / end_run helpers (fail-silent)

Later steps will add:

  trades      — log_trade helper (Stage 3)
  signals     — log_signal helper (Stage 5)
  risk        — preflight() gate (Stage 5)
  public_api/ — shared Public.com clients (Stage 5+)

Nothing in here imports from the existing Stock or Crypto bot repos. The
existing bots reach the League by importing a tiny adapter that wraps the
helpers in this package.
"""

__all__ = ["contracts", "status"]
