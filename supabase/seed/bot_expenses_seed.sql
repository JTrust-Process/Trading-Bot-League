-- ============================================================================
-- bot_expenses_seed.sql
--
-- Initial known recurring costs for the league. Run AFTER 012_bot_expenses.sql.
--
-- This file captures what we KNOW we're spending today. As subscriptions
-- change or new costs appear, add a row here, re-run, and the existing
-- rows update in place via the unique (bot_id, category, period) index.
--
-- Numbers as of 2026-05-21. Adjust if your actual bills differ.
--
-- Annualized vs. monthly:
--   - For tiny recurring costs (Anthropic API on Haiku, ~$1/yr) we record
--     a single annual row to avoid 12 near-zero rows.
--   - For monthly costs we record one row per month and re-run this file
--     monthly OR back-fill periodically.
--
-- IMPORTANT: this file is meant to be edited and re-run. Re-running is
-- idempotent — existing (bot_id, category, period) rows update; new
-- rows insert.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Fly.io hosting — agent_runner machine.
-- shared-cpu-1x 256MB always-on, billed at roughly $2/month after the
-- one-time $5 trial credit is exhausted. League-wide cost (bot_id NULL).
-- ---------------------------------------------------------------------------
insert into public.bot_expenses (
  bot_id, category, amount_usd, period, recurring, note
) values
  (NULL, 'fly_hosting', 2.00, '2026-05', true,
   'shared-cpu-1x 256MB always-on. $5 trial credit covers May fully.'),
  (NULL, 'fly_hosting', 2.00, '2026-06', true,
   'shared-cpu-1x 256MB always-on. Trial credit still partially active.'),
  (NULL, 'fly_hosting', 2.00, '2026-07', true,
   'shared-cpu-1x 256MB always-on. Estimated first fully-paid month.')
on conflict (coalesce(bot_id, '__league__'), category, period) do update set
  amount_usd = excluded.amount_usd,
  recurring  = excluded.recurring,
  note       = excluded.note;

-- ---------------------------------------------------------------------------
-- Anthropic API — agent_research_v1 daily Claude Haiku call.
-- ~$0.003/run × 250 weekday runs/year ≈ $0.75/year. League-wide attribution
-- (bot_id=NULL) so this row works even if agent_research_v1 isn't yet in
-- bot_registry. If you later run agent_research_v1_seed.sql and want to
-- re-attribute, update this row's bot_id manually.
-- ---------------------------------------------------------------------------
insert into public.bot_expenses (
  bot_id, category, amount_usd, period, recurring, note
) values
  (NULL, 'anthropic_api', 1.00, '2026', true,
   'agent_research_v1 — Haiku /v1/messages calls, ~$0.003/run × ~250 weekday fires.')
on conflict (coalesce(bot_id, '__league__'), category, period) do update set
  amount_usd = excluded.amount_usd,
  recurring  = excluded.recurring,
  note       = excluded.note;

-- ---------------------------------------------------------------------------
-- Claude subscription — Claude Pro/Max for Claude Desktop & MCP server.
-- Edit this row with your actual subscription tier once you confirm it.
-- League-wide (used by interactive sessions across both accounts).
-- Defaulting to $20/mo (Pro tier). Change to 100 or 200 if on Max.
-- ---------------------------------------------------------------------------
insert into public.bot_expenses (
  bot_id, category, amount_usd, period, recurring, note
) values
  (NULL, 'claude_subscription', 20.00, '2026-05', true,
   'Claude Pro — interactive AI trading via Claude Desktop MCP server. '
   || 'Update if subscription tier changes.')
on conflict (coalesce(bot_id, '__league__'), category, period) do update set
  amount_usd = excluded.amount_usd,
  recurring  = excluded.recurring,
  note       = excluded.note;

-- ---------------------------------------------------------------------------
-- OpenClaw — Anthropic token spend for the always-on agent.
-- Starts as a low estimate; revise upward once we see actual usage from
-- the agent_runner-hosted instance. Attribute to a virtual bot_id once
-- the public_openclaw_v1 entry exists in bot_registry.
-- ---------------------------------------------------------------------------
insert into public.bot_expenses (
  bot_id, category, amount_usd, period, recurring, note
) values
  (NULL, 'anthropic_api', 3.00, '2026-06', true,
   'OpenClaw on agent_runner Fly machine. Estimate; pending real usage data.')
on conflict (coalesce(bot_id, '__league__'), category, period) do update set
  amount_usd = excluded.amount_usd,
  recurring  = excluded.recurring,
  note       = excluded.note;

-- ---------------------------------------------------------------------------
-- Sanity check — show what we just wrote, grouped by period.
-- ---------------------------------------------------------------------------
select period,
       count(*) as rows,
       sum(amount_usd) as total_usd
from public.bot_expenses
group by period
order by period;
