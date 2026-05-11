-- ============================================================================
-- 008_bot_logs.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 007_bot_errors.sql.
--
-- Cross-bot sparse log mirror. Per the architecture (PLAN.md §2.6) we do
-- NOT mirror every stdout line into League — that would be too noisy.
-- Per-bot bot_logs in each existing project remains the verbose log. Only
-- WARN+ or tagged events cross over into this table when adapters/bots
-- explicitly call log_message().
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_logs (
  id         uuid primary key default gen_random_uuid(),
  bot_id     text not null references public.bot_registry(bot_id),
  run_id     uuid references public.bot_runs(id),
  logged_at  timestamptz not null default now(),
  level      text not null default 'INFO'
             check (level in ('DEBUG','INFO','WARN','ERROR')),
  event      text,              -- short tag, optional
  symbol     text,
  message    text not null
);

create index if not exists bot_logs_bot_time_idx
  on public.bot_logs (bot_id, logged_at desc);
create index if not exists bot_logs_level_idx
  on public.bot_logs (level);
create index if not exists bot_logs_run_idx
  on public.bot_logs (run_id);

alter table public.bot_logs enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_logs'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_logs
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_logs'
  ) then
    alter publication supabase_realtime add table public.bot_logs;
  end if;
end $$;
