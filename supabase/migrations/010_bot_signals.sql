-- ============================================================================
-- 010_bot_signals.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 009_bot_research_scores.sql.
--
-- Cross-bot signal stream. Bots write here whenever they identify a trade
-- idea (entry, exit, or watch). The dashboard's pending-signals queue and
-- the future agent_research bot's proposal stream both read from this
-- table.
--
-- Important: writing to bot_signals does NOT execute anything. A signal
-- is just information. An execution bot may consume signals via
-- bot_approvals (later migration) before acting on them; nothing in this
-- table by itself causes an order to be placed.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_signals (
  id                  uuid primary key default gen_random_uuid(),
  bot_id              text not null references public.bot_registry(bot_id),
  run_id              uuid references public.bot_runs(id),
  generated_at        timestamptz not null default now(),
  symbol              text,
  asset_class         text
                      check (asset_class is null or asset_class in (
                        'equity','etf','crypto','bond','option','option_spread'
                      )),
  signal_type         text not null,
                      -- examples: 'momentum','breakout','ema_cross','bond_screen',
                      -- 'short_setup','short_exit','options_idea','agent_proposal'
  direction           text
                      check (direction is null or direction in ('LONG','SHORT','NEUTRAL','EXIT')),
  confidence          numeric,
  suggested_size_usd  numeric,
  rationale           text,
  source              text,
                      -- examples: 'rules','public_bars','agent:claude','agent:perplexity'
  approval_required   boolean not null default false,
  metadata            jsonb not null default '{}'::jsonb
);

create index if not exists bot_signals_bot_time_idx
  on public.bot_signals (bot_id, generated_at desc);
create index if not exists bot_signals_symbol_idx
  on public.bot_signals (symbol);
create index if not exists bot_signals_type_idx
  on public.bot_signals (signal_type);
create index if not exists bot_signals_run_idx
  on public.bot_signals (run_id);

alter table public.bot_signals enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_signals'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_signals
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_signals'
  ) then
    alter publication supabase_realtime add table public.bot_signals;
  end if;
end $$;
