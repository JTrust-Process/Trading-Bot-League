-- ============================================================================
-- 002_bot_status.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 001_bot_registry.sql in the
-- same Supabase SQL editor session.
--
-- One row per bot. Upserted by every bot at the start and end of each run.
-- The cross-bot dashboard and the league_health workflow read this table
-- as the single source of truth for "is this bot alive?".
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_status (
  bot_id            text primary key references public.bot_registry(bot_id) on delete cascade,
  last_heartbeat_at timestamptz not null,
  last_run_id       uuid,
  last_run_status   text,                          -- mirrors bot_runs.status values
  last_error_at     timestamptz,
  last_error_msg    text,
  current_mode      text,                          -- echoed from registry at heartbeat time
  health            text not null default 'unknown'
                    check (health in ('healthy','degraded','down','unknown','muted')),
  details           jsonb not null default '{}'::jsonb,
  updated_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Indexes — supports the dashboard's "show me all bots ordered by health"
-- and the health monitor's "latest heartbeat" queries.
-- ---------------------------------------------------------------------------
create index if not exists bot_status_health_idx
  on public.bot_status (health);

create index if not exists bot_status_heartbeat_idx
  on public.bot_status (last_heartbeat_at desc);

-- ---------------------------------------------------------------------------
-- updated_at trigger (function defined in migration 001).
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_trigger where tgname = 'bot_status_updated_at'
  ) then
    create trigger bot_status_updated_at
      before update on public.bot_status
      for each row execute function public.set_updated_at();
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- RLS: anon read, service-role write.
-- ---------------------------------------------------------------------------
alter table public.bot_status enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_status'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_status
      for select using (true);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Optional: dashboard live updates (Supabase Realtime).
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_status'
  ) then
    alter publication supabase_realtime add table public.bot_status;
  end if;
end $$;
