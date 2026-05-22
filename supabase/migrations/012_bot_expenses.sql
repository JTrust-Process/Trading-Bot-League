-- ============================================================================
-- 012_bot_expenses.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 011_bot_approvals.sql.
--
-- League-wide expense ledger. Captures the recurring + one-off costs of
-- running the league so we can compute net P&L (trade gains − fees −
-- subscriptions − hosting) instead of looking at trades alone.
--
-- Two sources of expense data:
--
--   1. Manual entries here in `bot_expenses` — subscriptions, hosting,
--      Anthropic API credits, one-off fees. These don't have a clean
--      programmatic source so we enter them by hand once per month (or
--      annualized) and keep the table updated as costs change.
--
--   2. Per-trade fees, which already live in `bot_trades.fees_usd`. The
--      dashboard aggregates those at query time — we deliberately do NOT
--      duplicate them into this table.
--
-- bot_id is nullable: league-wide expenses (e.g., Fly hosting for the
-- agent_runner machine) aren't attributable to a single bot. If you can
-- attribute the cost cleanly (e.g., Anthropic API spend that's entirely
-- for agent_research_v1), set bot_id to that bot.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_expenses (
  id            uuid primary key default gen_random_uuid(),
  bot_id        text references public.bot_registry(bot_id),  -- nullable
  category      text not null
                check (category in (
                  'fly_hosting',          -- Fly.io VM costs
                  'anthropic_api',        -- direct API token spend (agent_research_v1, OpenClaw, etc.)
                  'claude_subscription',  -- Claude Pro/Max for Claude Desktop / MCP
                  'perplexity_subscription', -- Perplexity Max (if ever subscribed)
                  'openclaw_hosting',     -- if OpenClaw lives somewhere other than the main Fly app
                  'public_subscription',  -- any Public.com tier fees
                  'data_feed',            -- third-party market data
                  'other'
                )),
  amount_usd    numeric(10,2) not null,
  period        text not null,   -- 'YYYY-MM' for monthly, 'YYYY' for annual
  recurring     boolean not null default false,
  occurred_on   date,            -- when the charge actually hit (nullable for annualized)
  note          text,
  created_at    timestamptz not null default now()
);

create index if not exists bot_expenses_period_idx
  on public.bot_expenses (period);
create index if not exists bot_expenses_category_idx
  on public.bot_expenses (category);
create index if not exists bot_expenses_bot_idx
  on public.bot_expenses (bot_id);

-- One row per (bot_id, category, period) so re-running the seed updates
-- in place rather than creating duplicates. nullable bot_id is treated
-- as a distinct key value via coalesce.
create unique index if not exists bot_expenses_unique_period
  on public.bot_expenses (coalesce(bot_id, '__league__'), category, period);

-- ---------------------------------------------------------------------------
-- RLS: anon read, service-role write. Mirrors the rest of the schema.
-- ---------------------------------------------------------------------------
alter table public.bot_expenses enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_expenses'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_expenses
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_expenses'
  ) then
    alter publication supabase_realtime add table public.bot_expenses;
  end if;
end $$;
