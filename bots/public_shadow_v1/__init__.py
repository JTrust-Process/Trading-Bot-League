"""bots/public_shadow_v1 — shadow logger for Public.com brokerage account #2.

Polls the second account's trade history via Public's API and mirrors any
new trades into the League's `bot_trades` table so the dashboard can show
trades placed by AI tools (Claude MCP, OpenClaw, Perplexity Computer)
alongside the deterministic Python bots.

This bot never places orders. It is a one-way read from Public into the
League.
"""
