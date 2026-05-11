# Trading Bot League

Control plane for a portfolio of trading bots that share status, signals, trades, and a leaderboard while each bot keeps running independently in its own process / repo / Supabase project.

This repo is **only** the shared layer. The existing live bots are still in their own repos:

- **Stock bot** — `C:\Users\Jeremiah\source\Trading Bot\Trading Bot Project\` (live, hourly market-hours via GHA)
- **Crypto bot** — `C:\Users\Jeremiah\source\Crypto_Trading_Project\Crypto_Trading_Bot\` (live, every 15 min via GHA)
- **Health monitor + cross-bot dashboard** — `C:\Users\Jeremiah\source\repos\Bot_Monitoring_Dashboard\`

Those repos are **not modified by this repo**. The League adds a third Supabase project that the existing bots can optionally heartbeat into, plus a place for new paper / research / agent bots to live.

See `PLAN.md` for the full architecture, schema, risk framework, and roadmap.

---

## What's in here today (Step 1a — foundation only)

```
Trading Bot League/
├── PLAN.md                          full v1 plan (already approved)
├── README.md                        this file
├── .env.example                     names of env vars (no secrets)
├── .gitignore
├── league_core/                     shared Python helpers
│   ├── __init__.py
│   ├── contracts.py                 BotConfig dataclass + LeagueBot Protocol
│   └── status.py                    heartbeat / start_run / end_run
└── supabase/
    ├── migrations/
    │   ├── 001_bot_registry.sql     authoritative directory of bots
    │   └── 002_bot_status.sql       last-known liveness per bot
    └── seed/
        └── bot_registry_seed.sql    inserts for the two existing bots
```

There is **no order-placement code** here, no risk gate yet (Step 1a is foundation), and nothing in this repo runs against the live bots. Step 1b — adding a tiny fail-silent heartbeat to the existing bots — is a separate, explicitly-approved change that touches `Trading Bot/` and `Crypto_Trading_Project/`.

---

## Setup steps for you (one-time)

These are the manual steps you need to do; they're outside what code can do.

1. **Create the new Supabase project** on your second account (the first one is at the 2-project free-tier limit). Name it something like `trading-bot-league`.
2. **Copy the project URL and the service-role key** from the Supabase dashboard → Settings → API.
3. **Run the migrations in order** in the Supabase SQL editor:
   1. `supabase/migrations/001_bot_registry.sql`
   2. `supabase/migrations/002_bot_status.sql`
4. **Run the seed**: `supabase/seed/bot_registry_seed.sql`. This inserts the two existing live bots into `bot_registry` with their current real risk caps (`max_order_usd=15` for stock, `25` for crypto, etc. — all matching what's in your live workflows today).
5. **Add three new GitHub Secrets** to *each* of the existing bot repos (`Trading Bot` and `Crypto_Trading_Project`):
   - `LEAGUE_SUPABASE_URL`
   - `LEAGUE_SUPABASE_KEY` *(service-role key — same role as your existing per-project Supabase keys)*
   - `LEAGUE_BOT_ID` is **not** a secret — it goes in workflow `env:` blocks (`stock_momentum_v1` / `crypto_ema_atr_v1`)
6. **Tell me to proceed with Step 1b.** That's when I add the fail-silent heartbeat adapter to the two existing bots and wire the new env vars into their workflow YAML.

---

## Safety rules baked in from day one

These are codified in `PLAN.md` §8 and reflected in the code:

- The `league_core.status` helper is **fail-silent**. If `LEAGUE_SUPABASE_URL` is missing or the HTTP write errors, it prints to stdout and returns. The existing bot continues exactly as today.
- No new mandatory dependencies. The helper uses `requests` against the Supabase PostgREST API. Both existing bots already have `requests`.
- Migrations are idempotent (`create table if not exists`, `create index if not exists`, `do $$ if not exists … create policy …`). Safe to re-run.
- `.env` is gitignored. `.env.example` lists names only — no real values.
- This repo never touches order placement, sizing, symbols, schedules, or any risk env in the existing bots.

---

## File-by-file rundown

Every file in this repo at the end of Step 1a, why it exists, and what it touches:

| File | Purpose | Touches existing bots? |
|---|---|---|
| `PLAN.md` | The approved v1 plan. Reference doc. | no |
| `README.md` | This file. | no |
| `.env.example` | Names of env vars. No real secrets. | no |
| `.gitignore` | Standard ignores + `.env`. | no |
| `league_core/__init__.py` | Package marker. | no |
| `league_core/contracts.py` | `BotConfig` dataclass + `LeagueBot` Protocol — the runtime contract every bot satisfies. | no |
| `league_core/status.py` | Heartbeat / start_run / end_run helpers against the League Supabase project. Fail-silent. Lazy env reads. Pure `requests`. | no |
| `supabase/migrations/001_bot_registry.sql` | Creates `bot_registry`, its trigger, indexes, RLS, anon-read policy. | no — runs in the **new** League project only |
| `supabase/migrations/002_bot_status.sql` | Creates `bot_status`, indexes, RLS, anon-read policy. | no — same project |
| `supabase/seed/bot_registry_seed.sql` | Upserts `stock_momentum_v1` and `crypto_ema_atr_v1` rows with their **current** live caps. Idempotent. | no |

---

## What comes after Step 1a

Per `PLAN.md`:

- **Step 1b** *(needs your approval)*: add `league_status.py` adapter to the Stock repo and `crypto_bot/league/league_status.py` to the Crypto repo. Two-line additions in `bot.py` / `main.py` to call `heartbeat()` after `start_run()` and in `finally:`. Three env vars added to each workflow YAML. **No** changes to trading logic.
- **Stage 2 → Stage 7**: shared trade ledger mirroring, dashboard `/league` route, first paper bot (`etf_rotation_v1`), then the rest of the roadmap.

Everything proceeds one approval at a time.
