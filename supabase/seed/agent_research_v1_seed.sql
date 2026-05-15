-- ============================================================================
-- agent_research_v1_seed.sql
--
-- Register agent_research_v1 in bot_registry. Run AFTER 001_bot_registry.sql,
-- 010_bot_signals.sql, AND 011_bot_approvals.sql. Idempotent.
--
-- The CHECK constraint added by 001_bot_registry.sql physically rejects
-- bot_type='agent_research' WITH can_place_orders=true. The values below
-- are also what the bot's Python contracts enforce in BotConfig.__post_init__.
-- Triple-defense.
-- ============================================================================

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
  'agent_research_v1',
  'Agent Research v1 (Claude)',
  'agent_research',
  'research',
  'enabled',
  ARRAY['*']::text[],   -- the agent reads about everything; it doesn't write trades
  false,                -- HARD enforced by DB CHECK constraint
  true,                 -- every proposal goes through bot_approvals
  0,                    -- no order capacity at all
  0,                    -- no trades, ever
  0,                    -- no positions, ever
  'jeremiahallu13@gmail.com',
  'AI research bot. Reads the last 24h of league state, sends to Claude '
    || 'with a structured prompt, writes a daily brief + 0..3 pending '
    || 'approvals. Never imports an order client; the bot_registry CHECK '
    || 'constraint physically rejects can_place_orders=true for this row. '
    || 'Approvals are gates only — there is no execution consumer wired up '
    || 'yet, so an approved proposal is a "I would have done this" audit '
    || 'record until a future agent_execution_v1 bot consumes them.'
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

select bot_id, bot_name, mode, status, can_place_orders, bot_type
from public.bot_registry
where bot_id = 'agent_research_v1';
