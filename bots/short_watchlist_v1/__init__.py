"""bots/short_watchlist_v1 — bearish-setup detector + paper short simulator.

PAPER ONLY. This bot never calls any real order endpoint — no live short
orders, no live anything. It does two things:

  1. Identifies bearish setups in a curated equity/ETF universe and
     publishes them as SHORT signals to bot_signals.
  2. Simulates paper shorts at the latest bar close for each fresh setup,
     and simulates COVER on exit conditions. Trades go to bot_trades
     with is_paper=True; positions go to bot_positions with the same flag.

This is the first bot to exercise the SHORT and COVER side codes in the
schema. A future live short bot (short_v1) would be a separate package
with its own paper -> live graduation; this bot is permanently paper.
"""
