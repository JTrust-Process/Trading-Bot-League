"""bots/options_alert_v1 — research-only options strategy suggester.

RESEARCH-ONLY. This bot never simulates trades, never opens positions,
and has no order code path of any kind. It publishes strategy
suggestions ('options_idea' signals) for liquid optionable underlyings
based on volatility regime × trend regime combinations.

v1 limitation: we don't fetch real options chains. We derive everything
from the same daily bars we already use for the other bots — realized
vol over a rolling window, plus SMA-based trend regime — and map the
combination to a recommended strategy family (covered call, iron condor,
long puts, etc.).

A future v2 can swap in real chain data (via Public's options endpoint
or yfinance) to score specific strikes and expirations.
"""
