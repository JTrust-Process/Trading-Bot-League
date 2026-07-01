"""crypto_bot.league — Trading Bot League adapters for the Crypto bot.

Only one module today (league_status). Lives in its own sub-package to keep
the existing crypto_bot/ namespace clean and to make it obvious which files
are League-related vs core trading logic.

ADDITIVE only. Nothing in here touches order placement, strategy, sizing,
state, or any existing trading code path.
"""
