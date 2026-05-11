-- ============================================================================
-- 005_bot_positions.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 004_bot_trades.sql.
--
-- Cross-bot open + closed positions. Used primarily by NEW bots (paper /
-- research). The existing stock bot keeps its own `positions` table; we'll
-- mirror into here in a later substage if desired. The unique partial index
-- mirrors the audit fix already in the stock bot's positions table.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_positions (
  id              uuid primary key default gen_random_uuid(),
  bot_id          text not null references public.bot_registry(bot_id),
  symbol          text not null,
  asset_class     text not null
                  check (asset_class in (
                    'equity','etf','crypto','bond','option','option_spread'
                  )),
  status          text not null default 'open'
                  check (status in ('open','closed')),
  quantity        numeric,
  entry_price     numeric,
  entry_at        timestamptz,
  amount_usd      numeric,
  stop_loss       numeric,
  take_profit     numeric,
  exit_price      numeric,
  exit_at         timestamptz,
  pnl_usd         numeric,
  pnl_pct         numeric,
  close_reason    text,
  is_paper        boolean not null default false,
  metadata        jsonb not null default '{}'::jsonb,
  updated_at      timestamptz not null default now()
);

create unique index if not exists bot_positions_open_uniq
  on public.bot_positions (bot_id, symbol)
  where status = 'open';

create index if not exists bot_positions_bot_status_idx
  on public.bot_positions (bot_id, status);

-- updated_at trigger (function defined in migration 001).
do $$
begin
  if not exists (
    select 1 from pg_trigger where tgname = 'bot_positions_updated_at'
  ) then
    create trigger bot_positions_updated_at
      before update on public.bot_positions
      for each row execute function public.set_updated_at();
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- RLS: anon read, service-role write.
-- ---------------------------------------------------------------------------
alter table public.bot_positions enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_positions'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_positions
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_positions'
  ) then
    alter publication supabase_realtime add table public.bot_positions;
  end if;
end $$;
