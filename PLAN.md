# League of Trading Bots — v1 Architecture & Implementation Plan

**Status:** Draft v1 for review. No code changes have been made. Awaiting approval before any implementation step.

**Date:** 2026-05-10

---

## 0. What I found in your repos (inspection notes)

I have read-only access to the three existing project folders and have surveyed the relevant files. Concrete findings the plan is built on:

**Stock Bot — `C:\Users\Jeremiah\source\Trading Bot\Trading Bot Project\`**
- Flat Python layout: `bot.py`, `monitor.py`, `notify.py`, `strategy.py`, `momentum.py`, `breakout.py`, `market_data.py` (Polygon prices), `public_bars.py` (new Public bars), `indicators.py`, `backtest_public.py`, `analyze_backtests.py`, `backtest.py`, `list_symbols.py`, `scripts/finalize_runs.py`.
- GHA workflow `trading-bot.yml` runs `cron: "17 14-20 * * 1-5"` (hourly market-hours), 12-minute timeout, `MODE=live`, `RUN_ONCE=1`, posts a finalize step that always runs to close out `bot_runs` even on SIGKILL.
- `monitor.Monitor` class has the lifecycle pattern we want everywhere: `start_run / end_run / log_event / log_error / safe_execute` with exponential-backoff retries.
- Supabase tables in the Stock project: `trades`, `bot_runs (uuid PK, start_time/end_time/status/total_trades/total_errors/duration_ms/notes)`, `bot_events (event_type/symbol/metadata jsonb)`, `bot_errors (stage/symbol/error_type/message/severity/retry_count)`, `bot_logs (event/symbol/side/amount_usd/order_id/status/details)`, `positions`, `bot_state` (per-symbol JSONB mirror, audit migration `2026_05_08_audit_fixes.sql`).
- Risk envs already first-class: `MAX_ORDER_AMOUNT_USD`, `MAX_OPEN_POSITIONS`, `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN`, `LOSS_COOLDOWN_DAYS`, `MIN_HOLD_DAYS`, regime filter, ATR.
- New research layer (`public_bars.py`, `backtest_public.py`, `analyze_backtests.py`) is explicitly additive and never touches order placement — exactly the pattern we want to keep.

**Crypto Bot — `C:\Users\Jeremiah\source\Crypto_Trading_Project\Crypto_Trading_Bot\`**
- Modular package `crypto_bot/`: `core/engine.py`, `config/settings.py`, `data/{market_data,coingecko}.py`, `exchange/public_api.py`, `execution/trader.py`, `strategy/signal.py`, `risk/manager.py`, `state/{state,remote}.py`, `logging/{_supabase,logger,monitor,supabase_logger}.py`, `notifications/discord.py`, `utils/retry.py`. Entry: `main.py` with `try/except/finally` and explicit `had_error` flag (audit C1).
- GHA workflow `crypto_bot.yaml` runs `cron: "7,22,37,52 * * * *"`, 10-min timeout, 24/7, `DRY_RUN=0` live; cache-restored `state.json` with Supabase fallback in `bot_state`.
- Reconciles against Public's portfolio every cycle. ATR-driven sizing. Fail-closed on uncertainty.
- Supabase tables in the Crypto project: `bot_runs (bigserial PK, started_at/ended_at/status check)`, `bot_logs (run_id text, level, message)`, `bot_events (event_type/message)`, `bot_errors (context/error)`, `crypto_trades`, `bot_state`.

**Bot Monitoring Dashboard — `C:\Users\Jeremiah\source\repos\Bot_Monitoring_Dashboard\`**
- `monitor/health_check.py` reads both Supabase projects through per-bot `BotConfig` that abstracts schema column-name differences (`runs_started_col`, `errors_ts_col`, `runs_table`, etc.). Auto-restart and stuck-run cleanup are off by default — good safety culture.
- `dashboard/components/HealthDashboard.jsx` Next.js 15 app, brutalist UI, auto-refresh 60s, market-hours-aware suppression for the Stock bot.

**Critical finding driving the design:** the Stock and Crypto Supabase projects have **schema-incompatible** tables of the same names (`bot_runs.id` is `uuid` vs `bigserial`; `bot_events`, `bot_errors`, `bot_logs` have entirely different columns). We **cannot** unify under one shared schema by editing one of the existing projects without breaking the other bot. The plan below uses a **third "League" Supabase project** as the cross-bot control plane and leaves both existing per-bot schemas untouched. Existing bots only learn to send a tiny *additional* heartbeat to League.

---

## 1. Architecture plan

### 1.1 Repos & where each piece lives

We keep the existing repos and add a fourth — your current workspace `Trading Bot League/` becomes the **League control plane**.

```
Trading Bot League/                   (this repo — NEW, control plane)
  league_core/                        Python package: shared helpers
    __init__.py
    contracts.py                      LeagueBot Protocol + dataclasses
    status.py                         heartbeat/upsert helpers
    runs.py                           start_run / end_run helpers
    trades.py                         log_trade helper
    signals.py                        log_signal helper
    risk.py                           preflight checks
    public_api/                       (later) thin shared Public clients
      auth.py
      equities.py
      crypto.py
      bars.py
      options.py
      shorting.py
      bonds.py
  supabase/
    migrations/                       SQL for the League project (new project)
      001_bot_registry.sql
      002_bot_status.sql
      003_bot_runs.sql
      ...
    seed/
      bot_registry_seed.sql
  bots/                               new bots that don't fit existing repos
    etf_rotation_v1/
    bond_research_v1/
    short_watchlist_v1/
    options_alert_v1/
    multi_leg_options_paper_v1/
    agent_research_v1/
  scripts/
    leaderboard_snapshot.py
    reconcile_positions.py
  .github/workflows/
    leaderboard_snapshot.yml
    league_health.yml
  README.md
  .env.example

Trading Bot/Trading Bot Project/      (existing — UNCHANGED trading logic)
  ... existing files ...
  league_status.py                    NEW — small adapter that pings League
                                      (only added when you approve step 1)

Crypto_Trading_Project/Crypto_Trading_Bot/
  ... existing files ...
  crypto_bot/league/league_status.py  NEW — same idea, fits its package layout

repos/Bot_Monitoring_Dashboard/
  monitor/health_check.py             EDIT later (Stage 4): add a third
                                      BotConfig for the League project
  dashboard/                          EDIT later: add /league route that
                                      reads bot_registry/bot_status/etc.
```

### 1.2 Conceptual layers

```
L0  Brokerage           Public.com (orders, market data, accounts)
                        Polygon (stock prices), CoinGecko (crypto OHLC)
                              ▲
L1  Per-bot processes   stock_momentum_v1   crypto_ema_atr_v1
   (independent,        etf_rotation_v1     bond_research_v1
    deterministic)      short_watchlist_v1  options_alert_v1
                        multi_leg_options_paper_v1
                              ▲
                        agent_research_v1   ←  AI agents live ONLY here.
                        (writes signals,        Cannot place orders.
                         scores, ideas)
                              ▲
L2  League control      league_core (Python helpers)
    plane               League Supabase project (shared tables)
                        league_dashboard (UI)
                        leaderboard snapshot job
                              ▲
L3  Health & safety     Bot Health Monitor (extended to read League too)
                        Discord alerts
                        Manual approvals (bot_approvals)
                        Global kill switch
```

### 1.3 Bot categories and how they differ

| Category | Examples | Writes orders? | Writes `bot_signals`? | Writes `bot_trades`? | Writes `bot_research_scores`? |
|---|---|---|---|---|---|
| **Live execution** | `stock_momentum_v1`, `crypto_ema_atr_v1` | yes (deterministic) | yes (the order it placed) | yes | optional |
| **Paper execution** | `etf_rotation_v1`, `short_watchlist_v1`, `multi_leg_options_paper_v1` | no — simulates fills | yes | yes (with `is_paper=true`) | optional |
| **Research-only** | `bond_research_v1`, `options_alert_v1` | no | yes (proposals) | no | yes |
| **Agent research** | `agent_research_v1` | **never** | yes (proposals only) | no | yes |

The single hard line: **only "live execution" bots have any code path that calls `POST /order`**, and only after the shared `risk.preflight()` returns `ok=True`. Paper, research, and agent bots do not import the order client at all.

### 1.4 Public API integrations — where each piece slots in

We do **not** rewrite the existing Public clients. Instead `league_core/public_api/` is added as a *new* shared layer for *new* bots. The path:

- **Now:** Existing stock and crypto bots keep their own `public_api.py` and request helpers.
- **Stage 5+:** New bots (ETF rotation, bond research, etc.) import from `league_core/public_api/`. This isolates the new code from the live trading logic.
- **Eventually:** When the new shared client is proven by paper bots, we can *plan* a non-disruptive migration of existing bots — but that is a separate, explicitly-approved future step, not part of this plan.

| API surface | Used by | Where it goes in `league_core/public_api/` |
|---|---|---|
| Auth (token exchange) | All | `auth.py` — single source of truth for `PUBLIC_SECRET → access token` with refresh |
| Account / portfolio | Live, paper-recon, health | `accounts.py` |
| Equity / ETF orders | `stock_momentum_v1`, `etf_rotation_v1` (later live) | `equities.py` (paper bots stub the call) |
| Crypto orders | `crypto_ema_atr_v1` | `crypto.py` |
| Market data / historical bars | Research, backtests | `bars.py` (wraps the existing `public_bars.py` pattern) |
| Corporate bonds | `bond_research_v1` (read only) | `bonds.py` (no order surface exposed yet) |
| Short orders | `short_watchlist_v1` | not yet — research only emits `bot_signals` |
| Options & multi-leg | `options_alert_v1`, `multi_leg_options_paper_v1` | not yet — research only |
| Python SDK / CLI / Postman | dev / debugging | use as references; we wrap raw HTTP ourselves for control |

### 1.5 Where AI agents fit safely

AI agents (Claude MCP, OpenClaw, Perplexity) live in **one place**: the `agent_research_v1` bot. The contract is:

1. The agent runs in its own process, on its own schedule, in its own Supabase row of `bot_registry` with `bot_type='agent_research'`, `mode='research'`, `can_place_orders=false`, `manual_approval_required=true`.
2. The agent's only Supabase write surface is `bot_signals`, `bot_research_scores`, `bot_logs`, `bot_events`, `bot_errors`, and (optionally) `bot_approvals` rows with `status='pending'`.
3. **No execution bot ever reads from an agent's signal directly.** A human reviews `bot_approvals` rows in the dashboard, marks them `status='approved'`, and only then does an execution bot consider them — and even then only within its existing risk envelope.
4. `league_core/risk.py` enforces `bot_role != 'agent_research' OR can_place_orders == false` before any order ever leaves the wire.

This is the architectural guarantee that satisfies "AI agents may research, summarize, score, and propose ideas, but they cannot bypass risk controls."

---

## 2. Shared Supabase schema (League project)

These tables go in a **new** Supabase project (call it "League") that lives alongside the existing Stock and Crypto projects. They do **not** replace any existing per-bot tables. Existing bots keep writing their own `trades`/`crypto_trades`/`bot_runs`/etc. Until a bot is fully migrated, the League tables are a **roll-up**: the per-bot tables remain the source of truth, and the bot writes a small additional record into League so the cross-bot dashboard, leaderboard, and health monitor have one place to look.

Conventions:
- Every cross-bot table has a `bot_id text not null` foreign key to `bot_registry(bot_id)`.
- Primary keys are UUIDs (`gen_random_uuid()`) so no collisions with either existing project.
- All timestamps are `timestamptz` in UTC.
- `created_at` defaults to `now()` everywhere.
- RLS on, anon read, service-role write — same pattern as today.

### 2.1 `bot_registry` — the source of truth for what bots exist

```sql
create table bot_registry (
  bot_id                    text primary key,            -- e.g. 'stock_momentum_v1'
  bot_name                  text not null,               -- human-readable
  bot_type                  text not null,               -- 'stock' | 'crypto' | 'etf' | 'bond' | 'short' | 'options' | 'multi_leg_options' | 'agent_research'
  mode                      text not null
                            check (mode in ('research','paper','live')),
  status                    text not null default 'enabled'
                            check (status in ('enabled','disabled','killed')),
  -- Capabilities and limits
  allowed_instruments       text[] not null default '{}',-- e.g. ARRAY['SPY','QQQ']; '*' for any
  can_place_orders          boolean not null default false,
  manual_approval_required  boolean not null default true,
  max_order_usd             numeric not null default 0,
  max_daily_loss_usd        numeric,
  max_daily_loss_pct        numeric,
  max_daily_trades          int not null default 0,
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

-- updated_at maintenance
create or replace function set_updated_at() returns trigger
language plpgsql as $$ begin new.updated_at = now(); return new; end $$;
create trigger bot_registry_updated_at before update on bot_registry
  for each row execute function set_updated_at();

create index bot_registry_status_idx  on bot_registry (status);
create index bot_registry_mode_idx    on bot_registry (mode);
create index bot_registry_type_idx    on bot_registry (bot_type);
```

**Coexists with:** nothing — this is the new authoritative directory.

### 2.2 `bot_status` — last-known liveness for every bot

```sql
create table bot_status (
  bot_id            text primary key references bot_registry(bot_id) on delete cascade,
  last_heartbeat_at timestamptz not null,
  last_run_id       uuid,
  last_run_status   text,        -- 'running' | 'success' | 'warning' | 'failed'
  last_error_at     timestamptz,
  last_error_msg    text,
  current_mode      text,        -- echoed from registry at heartbeat time
  health            text not null default 'unknown'
                    check (health in ('healthy','degraded','down','unknown','muted')),
  details           jsonb not null default '{}'::jsonb,
  updated_at        timestamptz not null default now()
);
create index bot_status_health_idx     on bot_status (health);
create index bot_status_heartbeat_idx  on bot_status (last_heartbeat_at desc);
```

Single row per bot. Upserted by every bot at the start and end of each run — that gives both the dashboard and the health monitor a one-stop "is this bot alive?" check.

**Replaces:** the per-project age-of-last-run logic in `health_check.py` for new bots; for existing bots it complements the per-bot computation rather than replacing it.

### 2.3 `bot_runs` — cross-bot run lifecycle

```sql
create table bot_runs (
  id            uuid primary key default gen_random_uuid(),
  bot_id        text not null references bot_registry(bot_id),
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  status        text not null default 'running'
                check (status in ('running','success','warning','failed','timeout')),
  trade_count   int not null default 0,
  error_count   int not null default 0,
  duration_ms   bigint,
  trigger       text,           -- 'cron' | 'manual' | 'workflow_dispatch'
  git_sha       text,
  notes         text,
  metadata      jsonb not null default '{}'::jsonb
);
create index bot_runs_bot_started_idx on bot_runs (bot_id, started_at desc);
create index bot_runs_status_idx      on bot_runs (status);
```

**Coexists with:** Stock's `bot_runs` (uuid PK) and Crypto's `bot_runs` (bigserial PK). Existing bots keep writing their own. The League version just adds a parallel cross-bot row when wired in Stage 2.

### 2.4 `bot_events` — discrete domain events

```sql
create table bot_events (
  id          uuid primary key default gen_random_uuid(),
  bot_id      text not null references bot_registry(bot_id),
  run_id      uuid references bot_runs(id),
  occurred_at timestamptz not null default now(),
  event_type  text not null,    -- 'BUY','SELL','SIGNAL','REGIME_CHANGE','CIRCUIT_BREAKER','RECONCILE_DESYNC',...
  symbol      text,
  message     text,
  metadata    jsonb not null default '{}'::jsonb
);
create index bot_events_bot_time_idx on bot_events (bot_id, occurred_at desc);
create index bot_events_run_idx      on bot_events (run_id);
create index bot_events_type_idx     on bot_events (event_type);
```

**Coexists with:** both existing `bot_events` schemas. Unified cross-bot view; per-bot tables remain primary.

### 2.5 `bot_errors`

```sql
create table bot_errors (
  id          uuid primary key default gen_random_uuid(),
  bot_id      text not null references bot_registry(bot_id),
  run_id      uuid references bot_runs(id),
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
create index bot_errors_bot_time_idx  on bot_errors (bot_id, occurred_at desc);
create index bot_errors_severity_idx  on bot_errors (severity);
create index bot_errors_run_idx       on bot_errors (run_id);
```

### 2.6 `bot_logs` — stdout-mirror (use sparingly)

```sql
create table bot_logs (
  id         uuid primary key default gen_random_uuid(),
  bot_id     text not null references bot_registry(bot_id),
  run_id     uuid references bot_runs(id),
  logged_at  timestamptz not null default now(),
  level      text not null default 'INFO'
             check (level in ('DEBUG','INFO','WARN','ERROR')),
  event      text,              -- short tag, optional
  symbol     text,
  message    text not null
);
create index bot_logs_bot_time_idx on bot_logs (bot_id, logged_at desc);
create index bot_logs_level_idx    on bot_logs (level);
create index bot_logs_run_idx      on bot_logs (run_id);
```

We do **not** mirror every line of stdout to League — too noisy. Only `WARN+` and `event != null` cross over. Per-bot `bot_logs` stays the verbose log.

### 2.7 `bot_trades` — unified trade ledger

```sql
create table bot_trades (
  id            uuid primary key default gen_random_uuid(),
  bot_id        text not null references bot_registry(bot_id),
  run_id        uuid references bot_runs(id),
  occurred_at   timestamptz not null default now(),
  symbol        text not null,
  asset_class   text not null,   -- 'equity','etf','crypto','bond','option','option_spread'
  side          text not null check (side in ('BUY','SELL','SHORT','COVER')),
  quantity      numeric,
  price         numeric,
  amount_usd    numeric,
  fees_usd      numeric default 0,
  pnl_usd       numeric,
  pnl_pct       numeric,
  reason        text,
  strategy      text,
  is_paper      boolean not null default false,
  order_id      text,
  metadata      jsonb not null default '{}'::jsonb
);
create index bot_trades_bot_time_idx  on bot_trades (bot_id, occurred_at desc);
create index bot_trades_symbol_idx    on bot_trades (symbol);
create index bot_trades_paper_idx     on bot_trades (is_paper);
create unique index bot_trades_order_id_uq
  on bot_trades (bot_id, order_id) where order_id is not null;
```

**Coexists with:** Stock's `trades` and Crypto's `crypto_trades`. New bots write here as primary; existing bots mirror trades into League once Stage 3 is approved.

### 2.8 `bot_positions` — open + closed positions

```sql
create table bot_positions (
  id              uuid primary key default gen_random_uuid(),
  bot_id          text not null references bot_registry(bot_id),
  symbol          text not null,
  asset_class     text not null,
  status          text not null default 'open' check (status in ('open','closed')),
  quantity        numeric,
  entry_price     numeric,
  entry_at        timestamptz,
  amount_usd      numeric,
  stop_loss       numeric,
  take_profit     numeric,
  exit_price      numeric,
  exit_at         timestamptz,
  pnl_usd         numeric,
  pnl_pct         numeric,
  close_reason    text,
  is_paper        boolean not null default false,
  metadata        jsonb not null default '{}'::jsonb,
  updated_at      timestamptz not null default now()
);
create unique index bot_positions_open_uniq
  on bot_positions (bot_id, symbol) where status = 'open';
create index bot_positions_bot_status_idx on bot_positions (bot_id, status);
```

**Replaces:** for *new* bots only. Stock's existing `positions` table stays.

### 2.9 `bot_signals` — every signal a bot generates

This is the central table research bots and AI agents write to.

```sql
create table bot_signals (
  id              uuid primary key default gen_random_uuid(),
  bot_id          text not null references bot_registry(bot_id),
  run_id          uuid references bot_runs(id),
  generated_at    timestamptz not null default now(),
  symbol          text,
  asset_class     text,
  signal_type     text not null,  -- 'momentum','breakout','ema_cross','bond_screen','options_idea','short_setup','agent_proposal'
  direction       text check (direction in ('LONG','SHORT','NEUTRAL','EXIT')),
  confidence      numeric,        -- 0..1
  suggested_size_usd numeric,
  rationale       text,
  source          text,           -- 'rules','public_bars','agent:claude','agent:perplexity', etc.
  approval_required boolean not null default false,
  metadata        jsonb not null default '{}'::jsonb
);
create index bot_signals_bot_time_idx on bot_signals (bot_id, generated_at desc);
create index bot_signals_symbol_idx   on bot_signals (symbol);
create index bot_signals_type_idx     on bot_signals (signal_type);
```

### 2.10 `bot_research_scores` — symbol-level scoring snapshots

This is the structured output of `analyze_backtests.py`-style work, generalized.

```sql
create table bot_research_scores (
  id               uuid primary key default gen_random_uuid(),
  bot_id           text not null references bot_registry(bot_id),
  scored_at        timestamptz not null default now(),
  symbol           text not null,
  asset_class      text not null,
  period           text,            -- 'MONTH','QUARTER','HALF_YEAR','YEAR' or arbitrary
  score            numeric,         -- e.g. composite or PF
  classification   text,            -- 'keep_active','reduce_priority','paper_only','remove'
  metrics          jsonb not null default '{}'::jsonb,  -- win_rate, pf, max_dd, etc.
  notes            text
);
create index bot_research_scores_symbol_idx on bot_research_scores (symbol, scored_at desc);
create index bot_research_scores_bot_idx    on bot_research_scores (bot_id, scored_at desc);
```

### 2.11 `bot_leaderboard_snapshots`

```sql
create table bot_leaderboard_snapshots (
  id              uuid primary key default gen_random_uuid(),
  snapshot_at     timestamptz not null default now(),
  window          text not null,    -- '7d','30d','90d','ytd','all'
  bot_id          text not null references bot_registry(bot_id),
  trades          int not null default 0,
  wins            int not null default 0,
  losses          int not null default 0,
  win_rate        numeric,
  avg_win_pct     numeric,
  avg_loss_pct    numeric,
  profit_factor   numeric,
  max_drawdown_pct numeric,
  total_return_pct numeric,
  total_pnl_usd   numeric,
  consistency_score numeric,        -- e.g. share of positive periods
  rank            int,
  metadata        jsonb not null default '{}'::jsonb
);
create unique index bot_leaderboard_snapshot_uq
  on bot_leaderboard_snapshots (snapshot_at, window, bot_id);
create index bot_leaderboard_window_idx
  on bot_leaderboard_snapshots (window, snapshot_at desc);
```

Populated by a daily `scripts/leaderboard_snapshot.py` GHA job that aggregates `bot_trades`. Keeps the dashboard cheap to render.

### 2.12 `bot_approvals` — human-in-the-loop gate

```sql
create table bot_approvals (
  id               uuid primary key default gen_random_uuid(),
  bot_id           text not null references bot_registry(bot_id),
  signal_id        uuid references bot_signals(id),
  requested_at     timestamptz not null default now(),
  expires_at       timestamptz,
  action           text not null,   -- 'BUY','SELL','SHORT','COVER','OPTION_OPEN', etc.
  symbol           text,
  payload          jsonb not null,  -- full proposed order parameters
  status           text not null default 'pending'
                   check (status in ('pending','approved','rejected','expired','consumed')),
  approver_email   text,
  approver_note    text,
  decided_at       timestamptz
);
create index bot_approvals_status_idx on bot_approvals (status, requested_at desc);
create index bot_approvals_bot_idx    on bot_approvals (bot_id, requested_at desc);
```

Live execution bots check `bot_approvals` for `status='approved' AND consumed_at IS NULL` before acting on any agent-sourced or risky signal. They flip the row to `consumed` after acting.

### 2.13 Coexistence summary

| New table | Existing tables it coexists with | Replacement plan |
|---|---|---|
| `bot_registry` | none | new authoritative directory |
| `bot_status` | health monitor's in-memory derivation | adds a queryable single-row-per-bot truth |
| `bot_runs` | Stock `bot_runs (uuid)`, Crypto `bot_runs (bigserial)` | parallel mirror; no migration of existing |
| `bot_events` | Stock + Crypto `bot_events` | parallel mirror |
| `bot_errors` | Stock + Crypto `bot_errors` | parallel mirror |
| `bot_logs` | Stock `bot_logs (event/symbol/...)`, Crypto `bot_logs (level/message)` | parallel; only `WARN+` mirrored |
| `bot_trades` | Stock `trades`, Crypto `crypto_trades` | new bots primary here; existing mirror later |
| `bot_positions` | Stock `positions` | new bots primary here; Stock keeps its own |
| `bot_signals` | none | new |
| `bot_research_scores` | `analyze_ranking.csv` (Stock research output) | structured replacement; CSVs keep working |
| `bot_leaderboard_snapshots` | none | new |
| `bot_approvals` | none | new |

---

## 3. Bot interface standard

A bot in the League is anything that satisfies two things: a **registry row** and the **runtime contract** (5 lifecycle calls + 3 logging calls).

### 3.1 Registry row (declarative)

The schema in §2.1 captures every required field. A bot is registered by inserting one row into `bot_registry`. New bots ship a SQL file under `bots/<bot_id>/registry.sql` that does this insert.

### 3.2 Runtime contract (Python)

`league_core/contracts.py`:

```python
from typing import Protocol, Optional
from dataclasses import dataclass

@dataclass
class BotConfig:
    bot_id: str
    bot_name: str
    bot_type: str            # 'stock'|'crypto'|'etf'|'bond'|'short'|'options'|'multi_leg_options'|'agent_research'
    mode: str                # 'research'|'paper'|'live'
    allowed_instruments: list[str]
    can_place_orders: bool
    manual_approval_required: bool
    max_order_usd: float
    max_daily_loss: Optional[float]
    max_daily_trades: int

class LeagueBot(Protocol):
    config: BotConfig
    # Lifecycle
    def start_run(self, trigger: str = "cron") -> str: ...     # returns run_id
    def end_run(self, run_id: str, status: str = "success") -> None: ...
    def heartbeat(self, health: str = "healthy", details: Optional[dict] = None) -> None: ...
    # Outputs
    def log_signal(self, run_id: str, **fields) -> None: ...
    def log_trade(self, run_id: str, **fields) -> None: ...
    def log_error(self, run_id: str, stage: str, error: Exception,
                  symbol: Optional[str] = None, severity: str = "warning") -> None: ...
```

Implementation lives in `league_core/{status,runs,trades,signals}.py` — each bot composes the helpers; we don't force them to inherit. This way the existing `Monitor` classes in Stock and Crypto can keep working and we just **add** league-side calls alongside.

### 3.3 Adapter pattern for existing bots

For Stock and Crypto we add a tiny shim, e.g. `league_status.py` (Stock) / `crypto_bot/league/league_status.py` (Crypto) that:
- Reads `LEAGUE_SUPABASE_URL` / `LEAGUE_SUPABASE_KEY` / `LEAGUE_BOT_ID` from env.
- Exposes `heartbeat(health, details)`, `start_run(trigger)`, `end_run(run_id, status)`.
- **Fail-silent**: if env vars are missing or the HTTP write errors, prints and returns. Never raises into the bot.
- Uses `requests` against PostgREST so it has zero new dependencies (Stock already has `requests`; Crypto already has `requests`).

The existing `Monitor` lifecycle in each bot stays as-is. The shim is called *alongside* it, not in place of it.

---

## 4. Risk framework

### 4.1 Where risk lives

`league_core/risk.py` exposes one function:

```python
def preflight(bot_id: str, action: str, symbol: str, amount_usd: float,
              context: dict) -> tuple[bool, str]:
    """Return (allowed, reason). Fail-closed on any uncertainty."""
```

Every order placement in any League bot is required to call this immediately before placing the order. The function is pure-Python + a single read of `bot_registry` and `bot_status` (and recent `bot_trades` for daily-trade/loss tallies).

### 4.2 Rules enforced

The function refuses to return `(True, …)` if any of the following is true:

1. **Global kill switch:** environment variable `LEAGUE_KILL=1` OR `bot_registry.status='killed'` for any registered bot of `bot_type` with shared underlying account. (Implementation: a single row `('__global__','GLOBAL','meta','live','killed',…)` with synthetic ID.)
2. **Per-bot kill switch:** `bot_registry.status != 'enabled'` for this `bot_id`.
3. **Mode mismatch:** `mode != 'live'` and `action` is a real order. Paper bots simulate fills only.
4. **Symbol not allowed:** `symbol not in allowed_instruments` (unless `allowed_instruments == ['*']`).
5. **Order too large:** `amount_usd > max_order_usd`.
6. **Daily trade cap:** count of today's `bot_trades` for this `bot_id` ≥ `max_daily_trades`.
7. **Daily loss cap:** sum of today's realized `pnl_usd` ≤ `-max_daily_loss_usd` (or `pct` equivalent against starting equity from `bot_status.details.day_start_capital`).
8. **Exposure cap:** sum of open `bot_positions.amount_usd` for this `bot_id` + this order > `max_exposure_usd`.
9. **Capability gates:**
   - `bot_type='options'` and `can_place_orders=true` → **always** require `manual_approval_required=true` AND a fresh `bot_approvals` row with `status='approved'` for this signal.
   - `bot_type='short'` → same.
   - `bot_type='bond'` → no live order path exists yet; refuse.
   - `bot_type='agent_research'` → refuse always (`AI agents may research, summarize, score, and propose ideas, but they cannot bypass risk controls`).
10. **Approval-required:** if `manual_approval_required=true`, refuse unless an `approved & not consumed` `bot_approvals` row matches the proposed action's signal_id.
11. **Position reconciliation:** `bot_status.details.last_reconcile_ok != true` within last N minutes → refuse all BUYs (existing crypto pattern, generalized).

### 4.3 Order envelope (single Python entry point per asset class)

Once `preflight` returns `(True, …)` the bot calls `league_core/public_api/<class>.place_order(...)`. Per-class helpers double-check the asset-class-specific gates so it is impossible to call e.g. `equities.place_order` from a bot whose `bot_type` is `agent_research`.

### 4.4 Operator surface

- `LEAGUE_KILL=1` env override (read each cycle, no restart needed).
- Manual `update bot_registry set status='killed' where bot_id='...'` for per-bot kill.
- Manual `update bot_registry set status='enabled' where bot_id='...'` to resume.
- All transitions get a Discord ping via `notifications/discord.py`.

### 4.5 What this means for existing bots

The Stock and Crypto bots already enforce their own risk — `MAX_ORDER_AMOUNT_USD`, `DAILY_LOSS_LIMIT`, `LOSS_COOLDOWN_DAYS`, ATR-derived sizing, buying-power buffer, fail-closed reconciliation. **We do not replace those.** When we eventually plumb League risk into the existing bots, it sits *outside* the existing checks as an additional gate. Both must pass. The existing internal checks remain the source of bot-specific behavior.

---

## 5. League dashboard plan

### 5.1 Where it lives

Add a `/league` route to the existing **`Bot_Monitoring_Dashboard`** Next.js app. That repo already reads multiple Supabase projects through per-bot configs, the cyberpunk/brutalist UI is in place, Vercel deploy is configured, and you already know how to set anon-key env vars there. We just add a third Supabase config entry pointing at the new League project.

```
dashboard/
  app/
    page.tsx              (existing health page — keep)
    league/page.tsx       NEW — the new league dashboard
  components/
    HealthDashboard.jsx   (existing)
    LeagueDashboard.jsx   NEW
    LeagueStatusCard.jsx  NEW
    LeagueLeaderboard.jsx NEW
    LeagueSignalsQueue.jsx NEW
    LeagueApprovalsQueue.jsx NEW
  lib/
    supabaseLeague.ts     NEW — anon-key client for league project
```

### 5.2 Layout (single page with tabs/sections)

1. **Status grid (top).** One card per bot from `bot_registry`. Each card shows:
   - Name, mode badge (live / paper / research), bot_type icon
   - Health dot from `bot_status.health` (healthy / degraded / down / muted)
   - Last heartbeat age, last run status, last error one-liner
   - "kill" button (no live action — it just opens a confirm modal that updates `bot_registry.status='killed'` via service-role *write* function — gated behind a typed-confirmation)
   - Link out to that bot's own dashboard (Stock and Crypto already have one)

2. **Group rails.** Status cards grouped: **Live** | **Paper** | **Research**. Live block is sticky.

3. **Recent alerts.** Last 25 rows from `bot_errors` where `severity in ('warning','critical')`, joined to `bot_registry.bot_name`. Color-coded.

4. **Recent trades.** Last 25 rows from `bot_trades` (live + paper, badge to distinguish), filterable by bot.

5. **Open positions.** All `bot_positions` where `status='open'`. Sorted by absolute size; total exposure shown at top per asset class.

6. **Exposures by asset class.** Donut: equity / crypto / etf / bond / option, computed from open positions. Live vs paper toggle.

7. **Pending ideas (`bot_signals` + `bot_approvals`).** Three buckets:
   - **Options ideas** awaiting approval (from `options_alert_v1`)
   - **Short ideas** awaiting approval (from `short_watchlist_v1`)
   - **Bond research** picks (read-only — no approval gate, this is informational)
   Each idea shows: symbol, rationale, confidence, suggested size, "Approve / Reject" buttons that POST to a tiny Next.js Route Handler that uses the *server-side* service-role key (never exposed to the browser).

8. **Leaderboard.** Pulled from `bot_leaderboard_snapshots`, window selector (7d / 30d / 90d / YTD / all). Columns: rank, bot, mode, return, win rate, profit factor, max drawdown, consistency. Sortable.

9. **Footer.** Links to the existing Stock and Crypto dashboards (env vars `NEXT_PUBLIC_STOCK_DASHBOARD_URL`, `NEXT_PUBLIC_CRYPTO_DASHBOARD_URL` — already conventionally available in your monitor repo).

### 5.3 Approvals security

The Approve / Reject buttons must **never** put the service-role key in the browser. They go through a Next.js Route Handler under `app/api/approvals/[id]/route.ts` that runs server-side, validates a session (start with HTTP basic via Vercel password protection or a single shared `LEAGUE_APPROVAL_TOKEN`), then writes to Supabase with the service-role key from a server-only env var.

---

## 6. Bot migration plan (do not break current bots)

| Stage | Goal | Repos touched | Trading-logic change? | Approval gate |
|---|---|---|---|---|
| **0** | Spin up the League Supabase project, run migrations from `Trading Bot League/supabase/migrations/`. Create new GitHub Secrets `LEAGUE_SUPABASE_URL`, `LEAGUE_SUPABASE_KEY` in both bot repos. | League only | none | This plan + your "go" |
| **1** | Insert `bot_registry` rows for `stock_momentum_v1` and `crypto_ema_atr_v1`. | League only (SQL seed file) | none | This plan + your "go" |
| **2** | Add tiny `league_status.py` adapter to each existing bot. Call `league_status.heartbeat()` and `league_status.start_run/end_run()` *alongside* the existing `Monitor` calls. Fail-silent. Add the League secrets to each workflow's `env:`. | Stock repo + Crypto repo | **none** | Step 1 implementation — needs your explicit approval per the safety rules. The change does not touch trading logic, order placement, risk, or symbols. |
| **3** | Mirror trades into `bot_trades` (and positions for Stock into `bot_positions`) by adding a one-line append after the existing trade insert succeeds. Still fail-silent. | Stock + Crypto | **none** | Separate approval — touches the success path of trade logging, low-risk but worth a checkpoint. |
| **4** | Build the `/league` route in Bot_Monitoring_Dashboard, reading the League project. Extend `monitor/health_check.py` with a third `BotConfig` for the League schema so the existing Discord pings see all bots. | Bot_Monitoring_Dashboard | none | Separate approval. |
| **5** | Add the first new bot — `etf_rotation_v1` in **paper** mode (DRY_RUN). Lives under `Trading Bot League/bots/etf_rotation_v1/`. Writes directly to League tables. Does not touch existing bots. | League only | none | Standalone plan / approval per bot. |
| **6** | Evaluate scheduler reliability under three GHA crons (Stock hourly, Crypto 15-min, ETF rotation hourly). Look at GHA drift, restore-key reliability, league heartbeat freshness. Decision point. | none | none | Decision review. |
| **7** | Only if Stage 6 shows GHA is unreliable: evaluate Render Cron / a small Hetzner/Fly VPS for the 15-min crypto cron. Single-bot move first; rest stay on GHA. | Possibly Crypto | execution location only — **not** logic | Separate plan + approval. |

After Stage 7 the foundation is sufficient for the rest of the bot roadmap (§7).

---

## 7. Future bot roadmap (safe build order)

Each new bot ships behind its own `bot_registry` row, its own GHA workflow, its own `requirements.txt` if needed, and its own `README.md`. None of them are allowed to import from the existing Stock or Crypto bot code — they read shared helpers from `league_core/`.

| # | Bot | Mode at launch | Why this order | Major dependencies |
|---|---|---|---|---|
| 1 | `etf_rotation_v1` | **paper** | Lowest risk — uses Public bars (already a known surface from `public_bars.py`), trades only ETFs in a small allowlist, can reuse existing momentum scoring. Validates the whole League stack end-to-end with simulated fills. | `league_core.public_api.bars`, `league_core.signals` |
| 2 | `bond_research_v1` | **research** | Pure screener, no execution path at all — the safest possible new bot. Validates `bot_research_scores` and the dashboard's research surfaces. | Public corporate-bonds endpoint (read), screener rules |
| 3 | `short_watchlist_v1` | **paper** | Builds the "weak setup" detector and writes signals + paper trades. No live short orders. Validates shorting-specific risk gates without exposure. | `league_core.public_api.bars`, `league_core.signals`, paper-fill simulator |
| 4 | `options_alert_v1` | **research** | Read-only options-chain scanner. Surfaces ideas to `bot_signals` + `bot_approvals` (pending only). No execution. Validates the approval queue UI. | Public options chain endpoint (read) |
| 5 | `multi_leg_options_paper_v1` | **paper** | Defined-risk spread simulator. Uses `bot_approvals` end-to-end (idea → approval → simulated fill). Last paper-only step before any live options decision. | All of the above; spread payoff math |
| 6 | `agent_research_v1` | **research** | Built last so the human-approval queue, signal review surface, and Discord notifications are already battle-tested. The first agent only summarizes and proposes — no auto-execution under any circumstances. | Claude MCP / OpenClaw / Perplexity integrations |

Promotion (paper → live) for any bot is **never automatic**. It requires:
- ≥ N weeks of paper performance meeting pre-declared thresholds (recorded in `bot_research_scores`).
- A separate `live` registry row created with conservative `max_order_usd`, `max_daily_trades`, `max_daily_loss`.
- Explicit human approval via the dashboard.

---

## 8. Implementation rules (codified)

- **Foundation only first.** First implementation step is `bot_registry` + `bot_status` + `league_status.py` adapter + safe heartbeat in existing bots. Nothing else.
- **No live trading code is touched.** No edits to: order placement, signal logic, sizing, symbols, risk envs, fees, regime filters, ATR, cooldowns, anything that changes what gets traded or how.
- **Adapter, not rewrite.** Existing `Monitor` classes stay. We add `league_status.heartbeat()` / `start_run()` / `end_run()` calls alongside, never in place of.
- **Fail-silent at the league boundary.** If the League project is unreachable, the existing bot continues exactly as today. Specifically: catch `Exception`, print to stdout, return — never raise.
- **No new mandatory dependencies.** The adapter uses `requests` (already present in both repos) against the Supabase REST API. No `supabase-py` changes.
- **Migrations are idempotent.** `create table if not exists`, `create index if not exists`, and the `do $$ if not exists … create policy …` pattern from your existing `2026_05_08_audit_fixes.sql`.
- **Every PR explains every file changed.** No drive-by edits.
- **Secrets stay in GitHub Secrets.** `.env.example` lists names only. `.gitignore` covers `.env` in the new repo from day one.
- **Reversible.** Removing `league_status.py` and the two added env vars rolls the bot back to today's behavior with no residue.
- **README notes.** Every new file gets a short top-of-file docstring; every new directory gets a `README.md` explaining its purpose.

---

## 9. First implementation step — files I propose to add or edit

Subject to your approval before any code is written.

### 9.1 New files in `Trading Bot League/` (no risk — none of this runs against the live bots)

| Path | Purpose | Approx size |
|---|---|---|
| `README.md` | What this repo is, how it relates to the others | ~1 page |
| `.env.example` | Names of `LEAGUE_SUPABASE_URL`, `LEAGUE_SUPABASE_KEY`, `LEAGUE_BOT_ID`, `LEAGUE_KILL` | tiny |
| `.gitignore` | `.env`, `__pycache__/`, `.venv/`, `node_modules/` | tiny |
| `league_core/__init__.py` | package marker | empty |
| `league_core/contracts.py` | `BotConfig` dataclass + `LeagueBot` Protocol | ~50 lines |
| `league_core/status.py` | `heartbeat()`, `start_run()`, `end_run()` against the League Supabase project via `requests` to PostgREST. Lazy env reads. Fail-silent. | ~120 lines |
| `supabase/migrations/001_bot_registry.sql` | the §2.1 table + trigger + indexes + RLS + read policy | ~50 lines |
| `supabase/migrations/002_bot_status.sql` | the §2.2 table + indexes + RLS + read policy | ~30 lines |
| `supabase/seed/bot_registry_seed.sql` | inserts for `stock_momentum_v1` and `crypto_ema_atr_v1` matching their *current* live envs (`max_order_usd=15` for stock, `25` for crypto, etc.) | ~30 lines |
| `docs/PLAN.md` | this file (already present) | — |

This is everything for Step 1a — landing the foundation. After running the two migrations and the seed in the new League Supabase project, the registry is populated and the heartbeat helper is ready.

### 9.2 Files to *edit* in existing repos for Step 1b — wiring heartbeats only

This step **does** touch existing bot repos. Per your rules, I will not write any of these without explicit approval. The proposed edits are:

**Stock — `Trading Bot/Trading Bot Project/`**

| Path | Change | Touches trading logic? |
|---|---|---|
| `league_status.py` *(new)* | ~80-line adapter, mirrors `league_core/status.py` so the Stock repo doesn't need to import a sibling repo. Fail-silent. | no |
| `bot.py` | **2 line additions only**: import `league_status`, call `league_status.heartbeat("starting")` immediately *after* the existing `monitor.start_run()`, and call `league_status.heartbeat("idle", details={...})` at the very end of `main()`. Both calls wrapped in `try/except Exception: pass`. No other change. | no |
| `.github/workflows/trading-bot.yml` | Add `LEAGUE_SUPABASE_URL`, `LEAGUE_SUPABASE_KEY`, `LEAGUE_BOT_ID: stock_momentum_v1` to the `env:` block of the existing run step. No change to symbols, sizing, schedule, or any other env. | no |

**Crypto — `Crypto_Trading_Project/Crypto_Trading_Bot/`**

| Path | Change | Touches trading logic? |
|---|---|---|
| `crypto_bot/league/__init__.py` *(new)* | empty | no |
| `crypto_bot/league/league_status.py` *(new)* | same adapter, fitted to the package layout | no |
| `main.py` | **2 line additions only**: import the adapter, call `league_status.heartbeat("starting")` after `monitor.start_run()`, call `league_status.heartbeat("idle", details={...})` in the `finally:` block (after `monitor.end_run`). Wrapped in `try/except`. | no |
| `.github/workflows/crypto_bot.yaml` | Add the same three `LEAGUE_*` env vars to the existing `env:` block. No change to anything else. | no |

**Bot Monitoring Dashboard — no changes in Step 1.** The health monitor extension comes later in Stage 4.

### 9.3 What I will NOT do in Step 1

- Touch `bot.py`'s order-placement code (`place_order_buy`, `place_order_sell`, etc.) in either bot.
- Touch `risk/manager.py`, `strategy/signal.py`, `momentum.py`, `breakout.py`, `strategy.py`, or any sizing logic.
- Change any `MAX_*`, `MIN_*`, `STOP_LOSS_*`, `TAKE_PROFIT_*`, `RISK_*`, `MOMENTUM_*`, or `BREAKOUT_*` env value.
- Change `SYMBOLS`, `SCAN_SYMBOLS`, `MOMENTUM_SYMBOLS`, `REGIME_SYMBOL`, or any allowlist.
- Change schedules, timeouts, concurrency groups, or cache restore-keys.
- Touch the existing Stock or Crypto Supabase projects.

---

## 10. Open questions for you before Step 1

1. **League Supabase project:** create a brand new project, or reuse one of the existing two (and namespace via `bot_id`)? I strongly recommend a **new** project so the existing bot tables remain isolated and the schema-incompatibility we found in §0 doesn't bite us.
2. **Repo for new bots:** add new bots under `Trading Bot League/bots/<bot_id>/` (one repo, simpler ops), or each new bot in its own repo (more isolation, more Vercel/GHA setup)? My recommendation: one repo, one workflow per bot, so adding `etf_rotation_v1` is one folder + one YAML.
3. **`LEAGUE_BOT_ID` in workflow envs:** confirm `stock_momentum_v1` and `crypto_ema_atr_v1` as the canonical IDs.
4. **Approval rotation:** is the human approver for `bot_approvals` always you (`jeremiahallu13@gmail.com`), or do you want a multi-approver future-proofing today?

Answer those four and I'll prepare the Step 1 PR-equivalent (the file additions in §9.1) for your review without touching either existing bot. Then a separate approval for §9.2.
