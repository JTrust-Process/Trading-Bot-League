-- ============================================================================
-- bond_research_v1_seed.sql
--
-- Register the bond_research_v1 screener bot in bot_registry. Run AFTER
-- 001_bot_registry.sql + 009_bot_research_scores.sql. Idempotent.
--
-- This bot is research-only and the caps below reflect that — can_place_orders
-- is false, max_order_usd is 0. If a future operator ever flips this bot's
-- mode to 'live' the registry CHECK constraints would still permit it, but
-- the bot's code has no order surface so the change would be inert.
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
  'bond_research_v1',
  'Bond Research v1 (research-only)',
  'bond',
  'research',
  'enabled',
  ARRAY['SGOV','SHY','IEF','TLT','LQD','HYG','TIP','BND'],
  false,          -- research bots never write orders, ever
  true,           -- approval would be required if ever wired to a trader
  0,              -- no order capacity at all
  0,              -- no trade cap because no trades
  0,              -- no positions
  'jeremiahallu13@gmail.com',
  'Bond-ETF screener. Scores 8 bond ETFs across the duration/credit '
    || 'spectrum on a trend/momentum/stability/liquidity composite. '
    || 'Writes to bot_research_scores only. No trade surface; cannot be '
    || 'promoted to live in place — would need a separate bond_paper_v1 '
    || 'bot to consume these scores.'
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

select bot_id, bot_name, mode, status, can_place_orders
from public.bot_registry
where bot_id = 'bond_research_v1';
