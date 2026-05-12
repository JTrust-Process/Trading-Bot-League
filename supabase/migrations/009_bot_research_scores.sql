-- ============================================================================
-- 009_bot_research_scores.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 008_bot_logs.sql.
--
-- Symbol-level scoring snapshots — the structured output of research bots.
-- A research bot inserts one row per (symbol, period) per cycle. The
-- dashboard reads the most recent row per (bot_id, symbol, period) to
-- show "what's the bot saying about this symbol right now."
--
-- The 'classification' field uses the same four-bucket scheme as the
-- stock bot's analyze_backtests.py advisory output, so a future
-- promotion bot can consume scores from any research bot uniformly.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_research_scores (
  id              uuid primary key default gen_random_uuid(),
  bot_id          text not null references public.bot_registry(bot_id),
  run_id          uuid references public.bot_runs(id),
  scored_at       timestamptz not null default now(),
  symbol          text not null,
  asset_class     text not null
                  check (asset_class in (
                    'equity','etf','crypto','bond','option','option_spread'
                  )),
  period          text,
  score           numeric,
  classification  text
                  check (classification in (
                    'keep_active','reduce_priority','paper_only','remove'
                  )),
  metrics         jsonb not null default '{}'::jsonb,
  notes           text
);

create index if not exists bot_research_scores_symbol_idx
  on public.bot_research_scores (symbol, scored_at desc);
create index if not exists bot_research_scores_bot_idx
  on public.bot_research_scores (bot_id, scored_at desc);
create index if not exists bot_research_scores_class_idx
  on public.bot_research_scores (classification);
create index if not exists bot_research_scores_run_idx
  on public.bot_research_scores (run_id);

alter table public.bot_research_scores enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_research_scores'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_research_scores
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_research_scores'
  ) then
    alter publication supabase_realtime add table public.bot_research_scores;
  end if;
end $$;
