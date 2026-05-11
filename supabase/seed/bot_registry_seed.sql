-- ============================================================================
-- bot_registry_seed.sql
--
-- Seed rows for the two bots that already exist and are running live today.
-- Run AFTER 001_bot_registry.sql + 002_bot_status.sql.
--
-- Idempotent (uses upsert via ON CONFLICT). Safe to re-run after editing
-- the values below — re-running will update the existing rows in place
-- and leave the rest of the schema alone.
--
-- IMPORTANT: every numeric cap below MIRRORS the value currently set in
-- the bot's live workflow YAML. Do not raise these as part of seeding —
-- the safety policy says "do not increase risk settings". If you want to
-- change a cap later, edit it in BOTH the workflow YAML and here, then
-- re-run this file.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- stock_momentum_v1
--   Source workflow: Trading Bot/Trading Bot Project/.github/workflows/trading-bot.yml
--   MAX_ORDER_AMOUNT_USD=15, MAX_TRADES_PER_DAY=5, MAX_OPEN_POSITIONS=8
--   DAILY_LOSS_LIMIT=0.03, MAX_DRAWDOWN=0.08
--   Allowed instruments = union of SYMBOLS, SCAN_SYMBOLS, MOMENTUM_SYMBOLS
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
  max_daily_loss_pct,
  max_daily_trades,
  max_open_positions,
  owner_email,
  notes
) values (
  'stock_momentum_v1',
  'Public Stock Momentum / Breakout v1',
  'stock',
  'live',
  'enabled',
  ARRAY['QQQ','SCHB','SCHD','SGOV','SPY','VTI',
        'AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA'],
  true,
  false,
  15,        -- MAX_ORDER_AMOUNT_USD
  0.03,      -- DAILY_LOSS_LIMIT (per-symbol in the bot; tracked aggregate here)
  5,         -- MAX_TRADES_PER_DAY
  8,         -- MAX_OPEN_POSITIONS
  'jeremiahallu13@gmail.com',
  'Existing live stock bot. Tracked aggregate caps mirror trading-bot.yml '
    || 'as of 2026-05-10. Per-symbol DAILY_LOSS_LIMIT=0.03 and MAX_DRAWDOWN=0.08 '
    || 'are still enforced inside bot.py — League caps are an additional gate, '
    || 'not a replacement.'
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


-- ---------------------------------------------------------------------------
-- crypto_ema_atr_v1
--   Source workflow: Crypto_Trading_Project/Crypto_Trading_Bot/.github/workflows/crypto_bot.yaml
--   SYMBOLS=BTC, MAX_ORDER_AMOUNT_USD=25, MIN_BUYING_POWER_BUFFER=25
--   No per-day trade cap (uses COOLDOWN_CANDLES=4 and CIRCUIT_BREAKER_LOSSES=3)
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
  'crypto_ema_atr_v1',
  'Public Crypto EMA + ATR v1',
  'crypto',
  'live',
  'enabled',
  ARRAY['BTC'],   -- workflow currently runs BTC only; settings module supports BTC,ETH
  true,
  false,
  25,             -- MAX_ORDER_AMOUNT_USD
  0,              -- 0 = no explicit per-day cap (cooldown candles handle pacing)
  2,              -- BTC + ETH headroom; only BTC enabled today
  'jeremiahallu13@gmail.com',
  'Existing live crypto bot. Tracked caps mirror crypto_bot.yaml as of '
    || '2026-05-10. CIRCUIT_BREAKER_LOSSES=3 and COOLDOWN_CANDLES=4 are '
    || 'enforced inside crypto_bot/risk/manager.py — League caps are an '
    || 'additional gate, not a replacement.'
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
-- Sanity check — list what we just wrote.
-- (You can ignore this output; it's just for the SQL editor result pane.)
-- ---------------------------------------------------------------------------
select bot_id, bot_name, bot_type, mode, status, max_order_usd,
       max_daily_trades, max_open_positions, can_place_orders
from public.bot_registry
order by bot_id;
