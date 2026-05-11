-- ============================================================================
-- 003_bot_runs.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 002_bot_status.sql.
--
-- Cross-bot run lifecycle. The league_status adapter has already been
-- inserting/patching rows here from both existing bots since Step 1b —
-- those writes have been 404'ing because the table didn't exist yet. Once
-- this migration runs, the next scheduled run of each bot will populate
-- bot_runs successfully without any code change.
--
-- Coexistence:
--   Stock's per-project bot_runs(uuid PK, start_time/end_time/total_trades/...)
--   Crypto's per-project bot_runs(bigserial PK, started_at/ended_at/status)
--   This League bot_runs is a SEPARATE cross-bot mirror. Existing per-bot
--   tables remain the source of truth for each bot's internal logic.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_runs (
  id            uuid primary key default gen_random_uuid(),
  bot_id        text not null references public.bot_registry(bot_id),
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  status        text not null default 'running'
                check (status in ('running','success','warning','failed','timeout')),
  trade_count   int not null default 0,
  error_count   int not null default 0,
  duration_ms   bigint
                generated always as (
                  case
                    when ended_at is null then null
                    else (extract(epoch from (ended_at - started_at)) * 1000)::bigint
                  end
                ) stored,
  trigger       text,                 -- 'cron' | 'manual' | 'workflow_dispatch'
  git_sha       text,
  notes         text,
  metadata      jsonb not null default '{}'::jsonb
);

create index if not exists bot_runs_bot_started_idx
  on public.bot_runs (bot_id, started_at desc);
create index if not exists bot_runs_status_idx
  on public.bot_runs (status);

-- ---------------------------------------------------------------------------
-- RLS: anon read, service-role write.
-- ---------------------------------------------------------------------------
alter table public.bot_runs enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_runs'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_runs
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_runs'
  ) then
    alter publication supabase_realtime add table public.bot_runs;
  end if;
end $$;
