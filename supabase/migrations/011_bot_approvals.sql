-- ============================================================================
-- 011_bot_approvals.sql
--
-- DIALECT: PostgreSQL (Supabase). Run AFTER 010_bot_signals.sql.
--
-- Human-in-the-loop gate. Research bots and AI agent bots write rows
-- here when they propose an action that requires explicit human approval
-- before any execution bot may act on it.
--
-- The dashboard renders pending rows with Approve / Reject buttons that
-- POST to a server-side Next.js Route Handler. The Route Handler holds
-- the service-role key (never exposed to the browser) and is the only
-- thing that writes to this table from the dashboard side.
--
-- Until the agent_research_v1 bot ships, this table is allowed to be
-- empty — the pending-approvals dashboard section will simply show
-- "No pending approvals."
--
-- Idempotent. Safe to re-run.
-- ============================================================================

create table if not exists public.bot_approvals (
  id              uuid primary key default gen_random_uuid(),
  bot_id          text not null references public.bot_registry(bot_id),
  signal_id       uuid references public.bot_signals(id),
  requested_at    timestamptz not null default now(),
  expires_at      timestamptz,
  action          text not null,
                  -- examples: 'BUY','SELL','SHORT','COVER','OPTION_OPEN','OPTION_CLOSE'
  symbol          text,
  payload         jsonb not null default '{}'::jsonb,
                  -- the full proposed order parameters: side, quantity,
                  -- amount_usd, price guidance, strategy id, expiry, etc.
  status          text not null default 'pending'
                  check (status in ('pending','approved','rejected','expired','consumed')),
  approver_email  text,
  approver_note   text,
  decided_at      timestamptz
);

create index if not exists bot_approvals_status_idx
  on public.bot_approvals (status, requested_at desc);
create index if not exists bot_approvals_bot_idx
  on public.bot_approvals (bot_id, requested_at desc);
create index if not exists bot_approvals_signal_idx
  on public.bot_approvals (signal_id);

alter table public.bot_approvals enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename  = 'bot_approvals'
      and policyname = 'Allow public read'
  ) then
    create policy "Allow public read" on public.bot_approvals
      for select using (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname    = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'bot_approvals'
  ) then
    alter publication supabase_realtime add table public.bot_approvals;
  end if;
end $$;
