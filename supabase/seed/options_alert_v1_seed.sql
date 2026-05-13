-- ============================================================================
-- options_alert_v1_seed.sql
--
-- Register options_alert_v1 in bot_registry. Run AFTER 001_bot_registry.sql
-- and 010_bot_signals.sql. Idempotent.
--
-- Research-only bot. can_place_orders=false and max_order_usd=0 reflect
-- that there's no order surface. allowed_instruments lists the symbols
-- we scan; if it ever became a live trader (via a separate bot, not this
-- one), the registry caps would still be the first guard.
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
  'options_alert_v1',
  'Options Alert v1 (research-only)',
  'options',
  'research',
  'enabled',
  ARRAY['SPY','QQQ','IWM','AAPL','NVDA','TSLA'],
  false,           -- never trades
  true,            -- approval required if signals are ever consumed by a trader
  0,
  0,
  0,
  'jeremiahallu13@gmail.com',
  'Research-only options strategy suggester. Maps (trend regime × vol '
    || 'regime) to a defined-risk strategy family per underlying. Publishes '
    || 'one options_idea signal per symbol per cycle with approval_required=true. '
    || 'Cannot be promoted to live in place — would need a separate '
    || 'options_paper_v1 -> options_v1 pair built on real chain data.'
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
where bot_id = 'options_alert_v1';
