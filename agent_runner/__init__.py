"""agent_runner — always-on scheduler service.

Replaces the GHA cron workflows for the 5 research/paper bots plus the
league_health monitor. Runs as a single long-lived Python process on
Fly.io (or any container host), using APScheduler to fire each bot's
existing run_cycle() function on its previous cron cadence.

What does NOT live here:

  - stock_momentum_v1 — stays on GHA, untouched.
  - crypto_ema_atr_v1 — stays on GHA, untouched.

Those two are your live capital. They have their own proven workflow
files, their own state.json caches, and their own concurrency groups.
Moving them off GHA is a separate decision for another day.
"""
