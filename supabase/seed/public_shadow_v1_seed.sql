-- ============================================================================
-- public_shadow_v1_seed.sql
--
-- bot_registry rows for the shadow-logger bot AND its virtual target bots.
--
-- The shadow logger itself (public_shadow_v1) doesn't trade — it polls
-- account 2's history and writes trades under VIRTUAL bot_ids. Those
-- virtual bot_ids need their own bot_registry rows so the dashboard
-- knows their display name, mode, and instrument universe.
--
-- Initial setup: ONE virtual bot, `public_account2_v1`, captures all
-- account 2 trades. Later, if you run multiple AI tools simultaneously,
-- we can split into:
--   - public_claude_mcp_v1   (Claude Desktop MCP sessions)
--   - public_openclaw_v1     (always-on OpenClaw agent on Fly)
--   - public_perplexity_v1   (Perplexity Computer scheduled tasks)
--
-- Idempotent via ON CONFLICT. Safe to re-run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- public_shadow_v1 — the bot that does the polling. Mode 'research' since
-- it doesn't trade. allowed_instruments=NULL means "any" (we don't
-- restrict reads).
-- ---------------------------------------------------------------------------
insert into public.bot_registry (
  bot_id,
  bot_name,
  bot_type,
  mode,
  status,
  allowed_instruments,
  can_place_orders,
  manual_approval_required,
  max_order_usd,
  max_daily_trades,
  max_open_positions,
  owner_email,
  notes
) values (
  'public_shadow_v1',
  'Public Shadow Logger (account 2)',
  'agent_research',  -- closest existing bot_type; we'd add 'shadow' later if needed
  'research',
  'disabled',  -- DISABLED 2026-05-28: account #2 reassigned off-platform, MCP path dropped
  ARRAY[]::text[],  -- empty: shadow logger doesn't constrain instruments (read-only)
  false,    -- never places orders
  false,    -- no approvals — read-only
  0,
  0,
  0,
  'jeremiahallu13@gmail.com',
  'DISABLED 2026-05-28. Originally mirrored trades from Public brokerage '
    || 'account #2 into bot_trades under virtual bot_ids '
    || '(public_account2_v1). Disabled because account #2 was reassigned '
    || 'to an off-platform strategy and the Claude MCP path was dropped. '
    || 'Bot code remains in repo for future revival; flip status back to '
    || '''enabled'' and restore the scheduler job to re-activate.'
)
on conflict (bot_id) do update set
  bot_name                 = excluded.bot_name,
  bot_type                 = excluded.bot_type,
  mode                     = excluded.mode,
  status                   = excluded.status,
  allowed_instruments      = excluded.allowed_instruments,
  can_place_orders         = excluded.can_place_orders,
  manual_approval_required = excluded.manual_approval_required,
  max_order_usd            = excluded.max_order_usd,
  max_daily_trades         = excluded.max_daily_trades,
  max_open_positions       = excluded.max_open_positions,
  owner_email              = excluded.owner_email,
  notes                    = excluded.notes,
  updated_at               = now();

-- ---------------------------------------------------------------------------
-- public_account2_v1 — virtual bot. The shadow logger writes trades from
-- account 2 under this bot_id. From the dashboard's perspective it looks
-- like a regular bot. From reality it's a label for "whoever placed
-- this trade on account 2 — Claude MCP, OpenClaw, or you manually."
--
-- Mode = 'live' because trades on account 2 ARE real money. The risk
-- caps below mirror the size of account 2; tighten as you actually fund
-- and use it.
-- ---------------------------------------------------------------------------
insert into public.bot_registry (
  bot_id,
  bot_name,
  bot_type,
  mode,
  status,
  allowed_instruments,
  can_place_orders,
  manual_approval_required,
  max_order_usd,
  max_daily_trades,
  max_open_positions,
  owner_email,
  notes
) values (
  'public_account2_v1',
  'Public Account #2 (AI tools)',
  'agent_research',  -- bot_type that allows order placement; we set can_place_orders=false here anyway
  'live',
  'disabled',  -- DISABLED 2026-05-28: account #2 reassigned off-platform; no shadow logger feeding it
  ARRAY[]::text[],  -- empty: AI tools can trade anything Public allows. Public enforces, not us.
  false,    -- the LOGGER doesn't place orders; AI tools do. From bot_registry's POV: false.
  false,
  270,      -- cap mirrors current capital in account 2 ($270 from DoorDash). Raise as funded.
  10,       -- conservative cap on trades/day attributed to this virtual bot
  10,
  'jeremiahallu13@gmail.com',
  'Virtual bot — trades placed on Public account #2 by AI tools (Claude '
    || 'MCP, OpenClaw, Perplexity Computer) are mirrored here by '
    || 'public_shadow_v1. Caps are bookkeeping, not enforcement — '
    || 'enforcement lives on Public side via account size + AI tool '
    || 'guardrails.'
)
on conflict (bot_id) do update set
  bot_name                 = excluded.bot_name,
  bot_type                 = excluded.bot_type,
  mode                     = excluded.mode,
  status                   = excluded.status,
  allowed_instruments      = excluded.allowed_instruments,
  can_place_orders         = excluded.can_place_orders,
  manual_approval_required = excluded.manual_approval_required,
  max_order_usd            = excluded.max_order_usd,
  max_daily_trades         = excluded.max_daily_trades,
  max_open_positions       = excluded.max_open_positions,
  owner_email              = excluded.owner_email,
  notes                    = excluded.notes,
  updated_at               = now();

-- ---------------------------------------------------------------------------
-- Sanity check.
-- ---------------------------------------------------------------------------
select bot_id, bot_name, bot_type, mode, status, can_place_orders, max_order_usd
from public.bot_registry
where bot_id in ('public_shadow_v1', 'public_account2_v1')
order by bot_id;
