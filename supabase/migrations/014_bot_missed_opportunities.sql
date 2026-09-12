-- ============================================================================
-- 014_bot_missed_opportunities.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 013_bot_expenses_recurring.sql.
--
-- PHASE 1 OF THE AI LAYER — AND IT CONTAINS NO AI.
--
-- Every row here is a trade the bot DECLINED to make, captured at the moment
-- of rejection, plus what the symbol did afterwards. Nothing writes to this
-- table except an observer hook; nothing reads it to make a trading decision.
-- It exists to answer one question with evidence instead of intuition:
--
--     "When the bot skips a candidate, is it right?"
--
-- Until that question has an answer, any AI scoring layer would be tuning
-- against a target nobody has measured. This table is the measurement.
--
-- WHY THIS IS SAFE BY CONSTRUCTION
--   * No foreign keys to bot_registry. An FK would make a bad bot_name a
--     hard insert failure, and this table must never be able to fail a
--     trading cycle. (bot_id FK violations silently broke every
--     agent_research_v1 write for weeks — see that incident before adding
--     one here.)
--   * run_id is TEXT, not uuid, and not a foreign key. The stock bot's
--     monitor generates its run id locally; coupling this table to
--     bot_runs would create a write-ordering dependency inside the cycle.
--   * Nothing in this schema is referenced by risk.py, the order path, or
--     any sizing calculation.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_missed_opportunities (
  id                  uuid primary key default gen_random_uuid(),
  created_at          timestamptz not null default now(),

  -- Deliberately a plain text label with a default, NOT an FK. See above.
  bot_name            text not null default 'stock_momentum_v1',
  run_id              text,

  -- What was passed over.
  symbol              text not null,
  price               numeric,          -- often NULL: see note below
  signal_type         text,             -- 'momentum' | 'breakout' | NULL
  signal_strength     numeric,
  score               numeric,          -- momentum score at rejection
  regime              text,             -- 'bull' | 'bear' | 'unknown'
  rank                integer,          -- momentum rank at rejection

  -- Why it was passed over. skip_reason is the bot's own verbatim string;
  -- blocked_by_rule is a coarse machine-readable bucket for grouping.
  skip_reason         text not null,
  blocked_by_rule     text,

  indicators          jsonb default '{}'::jsonb,
  metadata            jsonb default '{}'::jsonb,

  -- Forward outcome, filled in later by the scorer job. NULL means "not yet
  -- scored" OR "not enough future data" — the two are distinguished by
  -- outcome_checked_at being null vs set.
  --
  -- ⚠ UNITS: DECIMAL RETURNS, NOT PERCENT.
  --
  --     +0.95%  is stored as  0.009524
  --     -3.40%  is stored as -0.034000
  --
  -- Computed as (close_at_horizon / close_at_baseline) - 1.0, rounded to 6
  -- decimal places. Decimal is the finance convention and composes correctly
  -- (returns multiply, percentages don't), which matters the moment anyone
  -- aggregates these.
  --
  -- Anything READING these columns must multiply by 100 to display a
  -- percentage. A dashboard rendering 0.009524 as "0.95%" is correct; one
  -- rendering it as "0.01%" has forgotten the conversion.
  --
  -- Do NOT "fix" a small-looking number here by scaling it at write time.
  -- 0.0095 is a correct +0.95% return, not a bug.
  outcome_checked_at  timestamptz,
  next_1d_return      numeric,
  next_5d_return      numeric,
  next_20d_return     numeric
);

-- Column comments live in the database itself, so anyone inspecting the
-- table in the Supabase UI or via \d+ sees the units without reading this
-- file. `comment on` is idempotent and safe to re-run.
comment on table public.bot_missed_opportunities is
  'Candidates a bot declined to trade, plus forward returns. Analytics only — '
  'nothing reads this to make a trading decision. Return columns are DECIMAL '
  '(0.009524 = +0.95%), not percent.';

comment on column public.bot_missed_opportunities.next_1d_return is
  'DECIMAL return 1 trading day after the skip. 0.009524 = +0.95%. NULL if unscored or insufficient data.';
comment on column public.bot_missed_opportunities.next_5d_return is
  'DECIMAL return 5 trading days after the skip. 0.047619 = +4.76%. NULL if unscored or insufficient data.';
comment on column public.bot_missed_opportunities.next_20d_return is
  'DECIMAL return 20 trading days after the skip. 0.190476 = +19.05%. NULL if unscored or insufficient data.';
comment on column public.bot_missed_opportunities.price is
  'Price at rejection. Usually NULL: the skip point in bot.py precedes the bar fetch. The scorer resolves the baseline from history instead.';
comment on column public.bot_missed_opportunities.blocked_by_rule is
  'Coarse bucket derived from skip_reason by league_core.missed_opps.classify_skip_reason. Use for GROUP BY; skip_reason is the verbatim string.';

-- NOTE ON `price` BEING FREQUENTLY NULL
--
-- The rejection point in bot.py sits BEFORE get_daily_bars() is called for
-- that symbol — the bot declines on signal grounds without ever fetching a
-- price. Recording one would mean adding a network call inside the entry
-- loop for symbols the bot has already rejected, which is exactly the kind
-- of incidental change to a trading hot path this phase must not make.
--
-- So price is captured only when it happens to already be known, and the
-- scorer resolves the baseline close from historical bars instead. A NULL
-- price here is normal and expected, not a defect.


-- ── Indexes ────────────────────────────────────────────────────────────────
-- Sized for the actual query shapes: dashboard reads newest-first, analysis
-- groups by symbol or reason, and the scorer scans for unscored rows.

create index if not exists bot_missed_opps_created_idx
  on public.bot_missed_opportunities (created_at desc);

create index if not exists bot_missed_opps_symbol_idx
  on public.bot_missed_opportunities (symbol);

create index if not exists bot_missed_opps_reason_idx
  on public.bot_missed_opportunities (blocked_by_rule);

create index if not exists bot_missed_opps_bot_idx
  on public.bot_missed_opportunities (bot_name, created_at desc);

-- The scorer's hot path: "unscored rows old enough to have an outcome."
-- Partial index keeps it small — scored rows drop out of the index entirely.
create index if not exists bot_missed_opps_unscored_idx
  on public.bot_missed_opportunities (created_at)
  where outcome_checked_at is null;


-- ── Row Level Security ─────────────────────────────────────────────────────
--
-- Matches the convention used by 009_bot_research_scores.sql and
-- 010_bot_signals.sql: RLS enabled, anon may read, writes require the
-- service role. The dashboard reads with the anon key, so read access must
-- exist or the new panel renders empty. Writes come from the bot, which
-- uses the service key.
--
-- If your other League tables do NOT have RLS enabled, delete this block
-- rather than enabling it here alone — a lone RLS table is how dashboards
-- silently lose access to exactly one panel.

alter table public.bot_missed_opportunities enable row level security;

drop policy if exists bot_missed_opps_read on public.bot_missed_opportunities;
create policy bot_missed_opps_read
  on public.bot_missed_opportunities
  for select
  using (true);


-- ── Verification ───────────────────────────────────────────────────────────
select
  count(*)                                          as total_rows,
  count(*) filter (where outcome_checked_at is null) as unscored,
  count(distinct symbol)                            as symbols
from public.bot_missed_opportunities;
