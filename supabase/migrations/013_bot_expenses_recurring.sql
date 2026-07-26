-- ============================================================================
-- 013_bot_expenses_recurring.sql
--
-- Adds recurrence to bot_expenses.
--
-- WHY (found in the 2026-07-25 dashboard audit, finding M9):
-- The expenses panel only counted a row toward the current month when its
-- `period` was an exact 'YYYY-MM' match, or a 'YYYY' annual entry spread
-- over twelve. So a MONTHLY subscription entered once — Claude Pro at
-- period '2026-05', for example — contributed $0 in June, July, and every
-- month after.
--
-- The consequence was not merely a missing number. The dashboard computes
--     net contribution = trade P&L - expenses
-- so as expenses silently drifted toward zero, "net contribution" drifted
-- UPWARD. The single figure you would use to judge whether the whole
-- operation is worth running got progressively more flattering, with no
-- signal that it was doing so.
--
-- After this migration, mark each subscription with recurring = true and it
-- counts every month from `period` until `period_end` (or indefinitely if
-- period_end is null).
--
-- Idempotent. Safe to re-run.
-- ============================================================================

alter table public.bot_expenses
  add column if not exists recurring  boolean not null default false,
  add column if not exists period_end text;

comment on column public.bot_expenses.recurring is
  'When true, this row contributes to EVERY month from `period` through '
  '`period_end` (inclusive), or indefinitely when period_end is null. '
  'Use for subscriptions and any other standing cost. When false the row '
  'counts only in its own `period`, which is correct for one-off charges.';

comment on column public.bot_expenses.period_end is
  'Optional YYYY-MM at which a recurring cost stops. Null means ongoing. '
  'Set this when you cancel a subscription rather than deleting the row, so '
  'historical months keep reporting accurately.';

-- Sanity: period_end, when present, must look like YYYY-MM and not precede
-- period. Cheap guard against a typo silently zeroing a cost line.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'bot_expenses_period_end_format'
  ) then
    alter table public.bot_expenses
      add constraint bot_expenses_period_end_format
      check (period_end is null or period_end ~ '^\d{4}-\d{2}$');
  end if;

  if not exists (
    select 1 from pg_constraint where conname = 'bot_expenses_period_order'
  ) then
    alter table public.bot_expenses
      add constraint bot_expenses_period_order
      check (period_end is null or period_end >= period);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Backfill: mark the known standing costs as recurring.
--
-- These are the rows seeded by bot_expenses_seed.sql. Adjust if your actual
-- subscriptions differ. Deliberately NOT a blanket update — a one-off charge
-- wrongly marked recurring would over-report forever, which is the opposite
-- failure but just as wrong.
-- ---------------------------------------------------------------------------
update public.bot_expenses
set recurring = true
where category in ('fly_hosting', 'claude_subscription', 'anthropic_api')
  and period ~ '^\d{4}-\d{2}$'
  and recurring = false;

-- ---------------------------------------------------------------------------
-- Verify.
-- ---------------------------------------------------------------------------
select category, period, period_end, recurring, amount_usd, note
from public.bot_expenses
order by recurring desc, period desc, category;
