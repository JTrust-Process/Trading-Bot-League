"""bots/agent_research_v1 — AI-assisted research bot.

RESEARCH-ONLY. By design and by multiple layers of enforcement, this bot
can never place a trade:

  1. Python: BotConfig.__post_init__ raises if bot_type='agent_research'
     and can_place_orders=True (see league_core/contracts.py).
  2. Database: a CHECK constraint on bot_registry rejects the same
     combination (see supabase/migrations/001_bot_registry.sql).
  3. Code: this package contains no order client and never imports one.
     The only Supabase writes are to bot_signals, bot_approvals,
     bot_events, bot_errors, bot_logs.

What it does each cycle: read the last 24h of platform state (research
scores, signals, open positions), send it to an LLM with a structured
prompt, parse 0-3 trade-idea proposals out of the response, write each
proposal into bot_signals AND bot_approvals (status='pending') so the
human approval queue surfaces them.

Approval flow: human reviews each proposal on the /league dashboard and
clicks Approve / Reject. Approval flips the row to 'approved' but does
NOT trigger execution — there is no execution bot wired to consume
approvals yet. For now, approval is a "I would have done this" record.
"""
