-- ============================================================================
-- 001_bot_registry.sql
--
-- DIALECT: PostgreSQL (Supabase). Run in the Supabase SQL editor of the new
-- "trading-bot-league" project. Some local SQL clients (dbtools / VS Code
-- SQLTools) parse against ANSI SQL or T-SQL by default and will flag
-- PostgreSQL-only constructs like `do $$ … end $$` and `create policy`.
-- Those warnings can be ignored — Supabase runs PostgreSQL 15+, which
-- accepts everything below.
--
-- Every statement is idempotent. Safe to re-run.
--
-- This is the AUTHORITATIVE DIRECTORY of bots. Every other table in the
-- League schema references bot_registry.bot_id.
--
-- Coexistence note: this table does NOT exist in the existing Stock or
-- Crypto Supabase projects. It is brand new. Existing per-bot tables in
-- those projects remain unchanged.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. updated_at trigger (shared utility)
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end
$$;

-- ---------------------------------------------------------------------------
-- 2. bot_registry table
-- ---------------------------------------------------------------------------
create table if not exists public.bot_registry (
  bot_id                    text primary key,
  bot_name                  text not null,
  bot_type                  text not null
                            check (bot_type in (
                              'stock','crypto','etf','bond','short',
                              'options','multi_leg_options','agent_research'
                            )),
  mode                      text not null
                            check (mode in ('research','paper','live')),
  status                    text not null default 'enabled'
                            check (status in ('enabled','disabled','killed')),
  -- Capabilities and limits
  allowed_instruments       text[] not null default '{}',
  can_place_orders          boolean not null default false,
  manual_approval_required  boolean not null default true,
  max_order_usd             numeric not null default 0,
  max_daily_loss_usd        numeric,
  max_daily_loss_pct        numeric,
  max_daily_trades          int not null default 0,    -- 0 = no explicit cap
  max_open_positions        int,
  max_exposure_usd          numeric,
  -- Source / ownership
  repo_url                  text,
  owner_email               text,
  notes                     text,
  -- Provenance
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 3. Hard-rule check: agent_research bots may NEVER place orders.
--    Triple-defense: enforced in Python (BotConfig.__post_init__), in the
--    risk preflight (later), and here in the database.
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'bot_registry_agent_no_orders'
  ) then
    alter table public.bot_registry
      add constraint bot_registry_agent_no_orders
      check (bot_type <> 'agent_research' or can_place_orders = false);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 4. updated_at trigger
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_trigger where tgname = 'bot_registry_updated_at'
  ) then
    create trigger bot_registry_updated_at
      before update on public.bot_registry
      for each row execute function public.set_updated_at();
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 5. Indexes
-- ---------------------------------------------------------------------------
create index if not exists bot_registry_status_idx on public.bot_registry (status);
create index if not exists bot_registry_mode_idx   on public.bot_registry (mode);
create index if not exists bot_registry_type_idx   on public.bot_registry (bot_type);

-- ---------------------------------------------------------------------------
-- 6. Row-level security: anon can read, only service-role can write.
--    Same pattern as your existing per-bot Supabase projects.
-- ---------------------------------------------------------------------------
alter table public.bot_registry enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_registry'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_registry
      for select using (true);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 7. Optional: dashboard live updates (Supabase Realtime)
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_registry'
  ) then
    alter publication supabase_realtime add table public.bot_registry;
  end if;
end $$;
