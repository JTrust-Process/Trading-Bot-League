-- ============================================================================
-- 006_bot_events.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 005_bot_positions.sql.
--
-- Cross-bot discrete events. Mirrors the per-bot bot_events tables in the
-- existing Supabase projects. The dashboard's recent-events feed reads
-- from here for the unified view.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_events (
  id          uuid primary key default gen_random_uuid(),
  bot_id      text not null references public.bot_registry(bot_id),
  run_id      uuid references public.bot_runs(id),
  occurred_at timestamptz not null default now(),
  event_type  text not null,        -- 'BUY','SELL','SIGNAL','REGIME_CHANGE','CIRCUIT_BREAKER',...
  symbol      text,
  message     text,
  metadata    jsonb not null default '{}'::jsonb
);

create index if not exists bot_events_bot_time_idx
  on public.bot_events (bot_id, occurred_at desc);
create index if not exists bot_events_run_idx
  on public.bot_events (run_id);
create index if not exists bot_events_type_idx
  on public.bot_events (event_type);

alter table public.bot_events enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_events'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_events
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_events'
  ) then
    alter publication supabase_realtime add table public.bot_events;
  end if;
end $$;
