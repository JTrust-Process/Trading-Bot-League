"""bots/etf_rotation_v1 — ETF rotation paper bot.

PAPER ONLY. This bot never calls Public's order endpoint. All "trades"
are simulated against the latest daily-bar close from Public's historical
bars API and recorded into bot_trades / bot_positions with is_paper=True.
"""
