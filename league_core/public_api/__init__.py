"""league_core.public_api — shared HTTP clients for Public.com.

Per PLAN.md §1.4, this is the shared API layer for NEW League bots. It
deliberately does NOT replace the existing stock bot's or crypto bot's
own clients — those continue to run unchanged.

Modules:
  auth.py       Token exchange + account resolution. Single source of
                truth for PUBLIC_SECRET → access token with refresh, and
                for PUBLIC_ACCOUNT_ID → resolved accountId.
  equities.py   Equity / ETF order placement (market BUY by notional,
                market SELL by quantity) + fill-price polling.

What's NOT here yet (intentionally):
  crypto.py     would wrap crypto endpoints. The existing crypto bot
                still has its own client; no need to duplicate until a
                second crypto-touching bot lands in league_core.
  options.py    research-only bots use bars; no live options path exists.
  bonds.py      bond_research_v1 is read-only; no order surface.
  shorting.py   short_watchlist_v1 is paper-only; no live short orders.

Each public function in here returns a structured dict, never raises.
Callers check `result["ok"]` to decide whether to proceed. This matches
the soft-failure style used everywhere else in league_core.
"""
