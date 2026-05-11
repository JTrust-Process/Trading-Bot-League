-- ============================================================================
-- etf_rotation_v1_seed.sql
--
-- Register the etf_rotation_v1 paper bot in bot_registry. Run AFTER
-- 001_bot_registry.sql is in place. Idempotent (upsert via ON CONFLICT).
--
-- Caps are intentionally conservative for a first paper bot. They are also
-- additive guards: even though this bot never places real orders, the caps
-- are recorded here so that IF we later flip can_place_orders=true and
-- mode=live, the risk preflight has a sane starting envelope.
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
  max_daily_loss_pct,
  max_daily_trades,
  max_open_positions,
  owner_email,
  notes
) values (
  'etf_rotation_v1',
  'ETF Rotation v1 (Paper)',
  'etf',
  'paper',
  'enabled',
  ARRAY['SPY','QQQ','VTI','SCHD','SGOV'],
  false,                -- paper-only; this bot never calls the order endpoint
  true,                 -- approval required if ever promoted to live
  250,                  -- max single order $ if ever promoted
  0.05,                 -- max 5% daily loss if ever promoted
  5,                    -- at most 5 trades per day (5 sells + buys ≈ 1 rebalance)
  5,                    -- equal to universe size
  'jeremiahallu13@gmail.com',
  'Paper-only ETF rotation. Risk-on basket (SPY/QQQ/VTI/SCHD) when SPY > '
    || 'SMA(50); 100% SGOV otherwise. Rebalances on regime change only.'
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
  max_daily_loss_pct       = excluded.max_daily_loss_pct,
  max_daily_trades         = excluded.max_daily_trades,
  max_open_positions       = excluded.max_open_positions,
  owner_email              = excluded.owner_email,
  notes                    = excluded.notes,
  updated_at               = now();

select bot_id, bot_name, mode, status, can_place_orders, max_order_usd
from public.bot_registry
where bot_id = 'etf_rotation_v1';
