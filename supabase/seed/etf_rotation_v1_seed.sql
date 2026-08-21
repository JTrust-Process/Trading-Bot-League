-- ============================================================================
-- etf_rotation_v1_seed.sql
--
-- Register etf_rotation_v1 in bot_registry. Run AFTER 001_bot_registry.sql.
--
-- ⚠ THIS BOT IS **LIVE** WITH REAL CAPITAL (promoted 2026-07-24).
--   It was originally seeded as paper, and this file still said so long
--   after that stopped being true.
--
-- ── SAFE TO RE-RUN, WITH ONE RULE ──────────────────────────────────────────
--
-- The ON CONFLICT clause deliberately does NOT update `mode`, `status`,
-- `can_place_orders`, or `manual_approval_required`.
--
-- WHY (bug found in the 2026-08-08 audit): this file advertised itself as
-- "Idempotent. Safe to re-run", and the seed-file convention documented in
-- bot_registry_seed.sql is to edit caps here and re-run. But the DO UPDATE
-- list included `mode`, and the INSERT values said 'paper'. So re-running
-- this file — the documented way to adjust a cap — would have silently
-- demoted a live bot to paper.
--
-- The failure mode after that demotion is nasty and completely silent:
-- run_cycle takes the simulated branch, writes fake closes against REAL
-- holdings, and flips the bot_positions rows to 'closed'. The actual shares
-- stay at Public with nothing tracking them — get_open_symbols no longer
-- returns them, so the next rebalance won't sell them either. Orphaned live
-- capital, no error anywhere.
--
-- Those four columns are OPERATIONAL STATE, not declarative config. They are
-- changed deliberately, by hand, with the UPDATE at the bottom of this file
-- — never as a side effect of re-running a seed.
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
  'ETF Rotation v1 (Live)',
  'etf',
  'live',
  'enabled',
  ARRAY['SPY','QQQ','VTI','SCHD','SGOV'],
  true,                 -- live since 2026-07-24; enforced by risk.preflight
  false,                -- no per-order manual approval
  110,                  -- pairs with ETF_PAPER_CAPITAL=100; see note below
  0.05,                 -- max 5% daily loss
  5,                    -- at most 5 opens per day (~1 rebalance)
  5,                    -- equal to universe size
  'jeremiahallu13@gmail.com',
  'LIVE ETF rotation. Risk-on basket (SPY/QQQ/VTI/SCHD) when SPY > '
    || 'SMA(50); 100% SGOV otherwise. Rebalances on regime change only.'
)
on conflict (bot_id) do update set
  bot_name                 = excluded.bot_name,
  bot_type                 = excluded.bot_type,
  -- mode                  INTENTIONALLY NOT UPDATED — operational state
  -- status                INTENTIONALLY NOT UPDATED — operational state
  -- can_place_orders      INTENTIONALLY NOT UPDATED — operational state
  -- manual_approval_required  INTENTIONALLY NOT UPDATED — operational state
  allowed_instruments      = excluded.allowed_instruments,
  max_order_usd            = excluded.max_order_usd,
  max_daily_loss_pct       = excluded.max_daily_loss_pct,
  max_daily_trades         = excluded.max_daily_trades,
  max_open_positions       = excluded.max_open_positions,
  owner_email              = excluded.owner_email,
  notes                    = excluded.notes,
  updated_at               = now();


-- ── One-time correction, 2026-08-08 ────────────────────────────────────────
--
-- Two separate problems in the existing row, both of which had to be fixed
-- for the bot to work correctly at all:
--
-- 1. can_place_orders = false on a LIVE bot. This was harmless only because
--    risk._evaluate_rules never actually read the column. Now that it does
--    (REASON_ORDERS_NOT_PERMITTED), leaving it false would block every BUY.
--
-- 2. The live row had max_order_usd = 110 (this file previously said 250 —
--    the file and the database had drifted). Meanwhile ETF_PAPER_CAPITAL
--    was unset, so the bot sized against its 1000.0 default:
--
--        bull basket:  1000 / 4 = $250   vs 110  -> REFUSED
--        bear basket:  1000 / 1 = $1000  vs 110  -> REFUSED
--
--    So NO order this bot ever constructed could pass its own risk gate.
--    It has been live and completely unable to trade since 2026-07-24.
--
--    THE CAP IS NOT THE BUG — the sizing was. Fixed by setting
--    ETF_PAPER_CAPITAL=100 in fly.toml, which puts both baskets inside the
--    existing 110 cap ($25/symbol bull, $100 bear). The cap stays where it
--    is deliberately: raising it to 1000 would have multiplied real
--    exposure tenfold to work around a misconfiguration.
--
--    The bot now also sizes down to whatever the registry cap says
--    (SIZING_CAPPED event), so the two can no longer silently disagree.
update public.bot_registry set
  mode                     = 'live',
  status                   = 'enabled',
  can_place_orders         = true,
  manual_approval_required = false,
  max_order_usd            = 110,
  updated_at               = now()
where bot_id = 'etf_rotation_v1';


select bot_id, bot_name, mode, status, can_place_orders,
       manual_approval_required, max_order_usd
from public.bot_registry
where bot_id = 'etf_rotation_v1';
