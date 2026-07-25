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
  'Options Alert v1 (research-only, DEPRECATED)',
  'options',
  'research',
  'disabled',      -- DISABLED 2026-07-24: family-level suggestions with no
                    -- downstream consumer; superseded by future options_paper_v1
  ARRAY['SPY','QQQ','IWM','AAPL','NVDA','TSLA'],
  false,           -- never trades
  true,            -- approval required if signals are ever consumed by a trader
  0,
  0,
  0,
  'jeremiahallu13@gmail.com',
  'DEPRECATED 2026-07-24. Originally: research-only options strategy '
    || 'suggester mapping (trend × vol regime) to defined-risk strategy '
    || 'families. Disabled because it produces family-level signals with '
    || 'no strikes/expirations/greeks and no downstream execution '
    || 'consumer — 606 signals in 72 days growing bot_signals for no '
    || 'benefit. Superseded by future options_paper_v1 (not yet built) '
    || 'using Public options-chain, greeks, strategy-quote, and multi-leg '
    || 'placement endpoints. Bot code remains in bots/options_alert_v1/ '
    || 'for reference. To revive: flip status back to enabled and '
    || 'restore the add_job block in agent_runner/scheduler.py.'
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
