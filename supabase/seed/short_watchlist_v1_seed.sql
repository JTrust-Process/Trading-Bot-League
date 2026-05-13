-- ============================================================================
-- short_watchlist_v1_seed.sql
--
-- Register short_watchlist_v1 in bot_registry. Run AFTER 001_bot_registry.sql
-- (and 010_bot_signals.sql if you want signals to land). Idempotent.
--
-- Caps reflect the paper-only mode AND act as a guard: even if someone
-- later flips can_place_orders=true by mistake, max_order_usd=100 keeps
-- the blast radius tiny.
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
  'short_watchlist_v1',
  'Short Watchlist v1 (Paper)',
  'short',
  'paper',
  'enabled',
  ARRAY['AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA',
        'QQQ','SPY','IWM','XLK'],
  false,
  true,
  100,
  11,                 -- worst case: one open+close per symbol per day
  11,
  'jeremiahallu13@gmail.com',
  'Paper-only short watchlist. Detects bearish setups (price < SMA50 & '
    || 'SMA200, 20-day low breakdown, negative 3m momentum) and simulates '
    || 'SHORT entries; covers on SMA20 reversal, 5% stop, or 10% take-profit. '
    || 'Never places real short orders. Live shorting would require a '
    || 'separate short_paper_v1 -> short_v1 pair.'
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

select bot_id, bot_name, mode, status, can_place_orders, max_order_usd
from public.bot_registry
where bot_id = 'short_watchlist_v1';
