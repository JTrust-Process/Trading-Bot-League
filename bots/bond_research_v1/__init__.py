"""bots/bond_research_v1 — bond-ETF research screener.

RESEARCH-ONLY. This bot never simulates trades and never places orders.
It scores a fixed universe of bond ETFs using Public's historical bars
and publishes the scores to bot_research_scores.

The output is consumed by humans (via the /league dashboard) and may be
consumed by future bots (e.g. a bond_paper_v1 that picks from these
scores when deciding what to "buy"). This bot does not import or call
any order endpoint.
"""
