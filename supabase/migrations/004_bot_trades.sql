-- ============================================================================
-- 004_bot_trades.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 003_bot_runs.sql.
--
-- Unified cross-bot trade ledger. Existing bots will mirror their trades
-- into this table from Step 1c (Stage 3 trade-mirror plumbing). New bots
-- will write here as their primary trade store.
--
-- Coexistence:
--   Stock's per-project `trades` table — remains primary for the stock bot.
--   Crypto's per-project `crypto_trades` table — remains primary for crypto.
--   This `bot_trades` is the cross-bot view used by the league dashboard
--   and the leaderboard snapshot job (later stage).
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_trades (
  id            uuid primary key default gen_random_uuid(),
  bot_id        text not null references public.bot_registry(bot_id),
  run_id        uuid references public.bot_runs(id),
  occurred_at   timestamptz not null default now(),
  symbol        text not null,
  asset_class   text not null
                check (asset_class in (
                  'equity','etf','crypto','bond','option','option_spread'
                )),
  side          text not null
                check (side in ('BUY','SELL','SHORT','COVER')),
  quantity      numeric,
  price         numeric,
  amount_usd    numeric,
  fees_usd      numeric default 0,
  pnl_usd       numeric,
  pnl_pct       numeric,
  reason        text,
  strategy      text,
  is_paper      boolean not null default false,
  order_id      text,
  metadata      jsonb not null default '{}'::jsonb
);

create index if not exists bot_trades_bot_time_idx
  on public.bot_trades (bot_id, occurred_at desc);
create index if not exists bot_trades_symbol_idx
  on public.bot_trades (symbol);
create index if not exists bot_trades_paper_idx
  on public.bot_trades (is_paper);
create index if not exists bot_trades_run_idx
  on public.bot_trades (run_id);

-- Defense-in-depth: if Public's order_id is present, do not double-count.
-- Mirrors the same constraint in the existing crypto_trades table.
create unique index if not exists bot_trades_order_id_uq
  on public.bot_trades (bot_id, order_id)
  where order_id is not null;

-- ---------------------------------------------------------------------------
-- RLS: anon read, service-role write.
-- ---------------------------------------------------------------------------
alter table public.bot_trades enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_trades'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_trades
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_trades'
  ) then
    alter publication supabase_realtime add table public.bot_trades;
  end if;
end $$;
