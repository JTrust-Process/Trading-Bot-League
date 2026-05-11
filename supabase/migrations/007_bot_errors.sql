-- ============================================================================
-- 007_bot_errors.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 006_bot_events.sql.
--
-- Cross-bot error log. Mirrors the per-bot bot_errors tables. Used by the
-- dashboard's "recent alerts" panel and by the league_health workflow
-- (later stage) to decide when to ping Discord.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_errors (
  id          uuid primary key default gen_random_uuid(),
  bot_id      text not null references public.bot_registry(bot_id),
  run_id      uuid references public.bot_runs(id),
  occurred_at timestamptz not null default now(),
  stage       text,             -- 'auth','quote','order','strategy','reconcile','log',...
  symbol      text,
  error_type  text,
  message     text not null,
  severity    text not null default 'warning'
              check (severity in ('info','warning','critical')),
  retry_count int not null default 0,
  metadata    jsonb not null default '{}'::jsonb
);

create index if not exists bot_errors_bot_time_idx
  on public.bot_errors (bot_id, occurred_at desc);
create index if not exists bot_errors_severity_idx
  on public.bot_errors (severity);
create index if not exists bot_errors_run_idx
  on public.bot_errors (run_id);

alter table public.bot_errors enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_errors'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_errors
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_errors'
  ) then
    alter publication supabase_realtime add table public.bot_errors;
  end if;
end $$;
