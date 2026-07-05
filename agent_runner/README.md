# agent_runner

Always-on Python service that runs the League's research and paper bots
on an internal APScheduler. Replaces the GHA cron workflows for those
bots so they don't compete with the live stock and crypto bots for
GitHub Actions free minutes.

## What runs here

| Job | Schedule | Source |
|---|---|---|
| `bond_research_v1`    | weekday 14:35 UTC                              | `bots.bond_research_v1.main`    |
| `options_alert_v1`    | weekday 14:43 UTC                              | `bots.options_alert_v1.main`    |
| `agent_research_v1`   | weekday 14:50 UTC                              | `bots.agent_research_v1.main`   |
| `etf_rotation_v1`     | weekday hourly 14-20 UTC at :33                | `bots.etf_rotation_v1.main`     |
| `short_watchlist_v1`  | weekday hourly 14-20 UTC at :41                | `bots.short_watchlist_v1.main`  |
| `stock_momentum_v1`   | weekday hourly 14-20 UTC at :17 (**gated**)    | `bots.stock_momentum_v1.main`   |
| `crypto_ema_atr_v1`   | every 15 min, 24/7 at `:07/:22/:37/:52` (**gated**) | `bots.crypto_ema_atr_v1.main` |
| `league_health`       | every 15 min, 24/7 (`:09/:24/:39/:54`)         | `scripts.league_health.main`    |

`public_shadow_v1` was previously scheduled every 10 min, 24/7. It was
disabled 2026-05-28 (account #2 reassigned off-platform; Claude MCP path
dropped). Bot code remains in `bots/public_shadow_v1/` for future revival;
to re-enable, restore the `add_job` block in `scheduler.py` from git
history and flip `bot_registry.status` back to `'enabled'`.

`stock_momentum_v1` and `crypto_ema_atr_v1` were vendored into this repo
on 2026-06-01 as part of the GHA → Fly migration. They are **gated
behind the `LIVE_BOTS_ENABLED` env var** — `LIVE_BOTS_ENABLED=1` (or
`true`/`yes`/`on`) to schedule them, unset (or anything else) to leave
them unscheduled on Fly. See the Cutover section below for the safe
sequence to flip this on.

## What does NOT run here (yet)

Nothing! Both live bots have been vendored and are ready to run on Fly
once you complete the cutover procedure below. Until then they continue
to run on their existing GHA workflows in their original repos.

Those two stay on GHA on purpose. The agent_runner is intentionally
isolated from your live trading bots so a problem here can't take them
down.

## Architecture

```
agent_runner/
├── scheduler.py        ← entry point. APScheduler BlockingScheduler.
├── requirements.txt
├── Dockerfile          ← context = repo root
├── fly.toml            ← Fly.io deploy config
└── README.md           ← you are here
```

Each scheduled job calls the bot's existing `run_cycle()` function. The
bot code is not modified — `python -m bots.<name>.main` still works for
local testing exactly as before. The only thing the agent_runner does is
fire those functions on a schedule from a single long-lived process.

### Concurrency model

`max_workers=1` on the scheduler's threadpool executor. Reason: the bots
read `LEAGUE_BOT_ID` from `os.environ`, and the scheduler swaps the env
var per-job. Running two jobs in parallel would race on that env var. We
have at most 6 jobs and they each finish in a few seconds; serial is fine.

### State

Bot state files (`bots/etf_rotation_v1/state.json`, etc.) live inside
the container filesystem. They persist for the lifetime of the container
— effectively forever in a stable deploy — and reset on container
restart. The bots self-heal on reset (regime is re-derived from bars;
positions are read from Supabase, not state.json).

No Fly volumes are configured. Volumes cost a bit and we don't need
them; losing state.json across a deploy is a tolerated minor cost.

## Local development

From the **repo root**:

```bash
pip install -r agent_runner/requirements.txt
pip install -r bots/etf_rotation_v1/requirements.txt   # already a superset
export LEAGUE_SUPABASE_URL=...
export LEAGUE_SUPABASE_KEY=...
export PUBLIC_SECRET=...
export ANTHROPIC_API_KEY=...
python -m agent_runner.scheduler
```

The scheduler will print its startup banner and the next-run-time for
each job, then block waiting for the first scheduled fire. Ctrl+C exits
cleanly.

To trigger a job immediately for testing, run it standalone (the bot
modules still work as command-line entry points):

```bash
python -m bots.etf_rotation_v1.main
```

## Deploy to Fly.io

**Important**: `fly.toml` lives at the **repo root**, NOT inside
`agent_runner/`. Fly's CLI insists on treating fly.toml's directory as
the build context, and we need the build context to be the repo root so
the Dockerfile can `COPY bots/`, `COPY league_core/`, and `COPY scripts/`.

One-time setup, from the **repo root**:

```bash
# Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
fly auth login

# Initialize the app (does not deploy):
fly launch --copy-config --no-deploy
```

When `fly launch` prompts:

- App name: accept the suggestion or use `trading-bot-league-agent-runner`
- Region: `iad` (close to NYSE / Public's servers) or pick the closest
- Do NOT create a Postgres or Redis instance
- Do NOT deploy yet

Set the secrets (PowerShell users: put it all on one line, or use
backticks `` ` `` for line continuation instead of `\`):

```bash
fly secrets set \
  LEAGUE_SUPABASE_URL='https://...' \
  LEAGUE_SUPABASE_KEY='eyJ...' \
  LEAGUE_SUPABASE_ANON_KEY='eyJ...' \
  LEAGUE_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' \
  PUBLIC_SECRET='...' \
  ANTHROPIC_API_KEY='sk-ant-api03-...'
```

Then deploy:

```bash
fly deploy
```

Tail logs to confirm it's running:

```bash
fly logs
```

You should see the startup banner listing all 6 jobs with their next-run
times, then quiet until the next scheduled cron tick.

## Cutover — moving stock_momentum_v1 and crypto_ema_atr_v1 from GHA to Fly

Vendor phase is complete on 2026-06-01 (source lives here now; smoke
tests passed). This section is the runbook for actually cutting each
live bot over. **Danger window**: any period during which both GHA and
Fly are firing the same cron for the same bot. Same-minute duplicates
dedupe via the deterministic `uuid5` order_id (see `bot.py`
`deterministic_order_id`), but cross-minute duplicates would both fill.

Do stock first (weekdays 14-20 UTC only — cut over on a weekend, plenty
of dead time), then crypto (24/7 — pick a quiet window and tail logs).

### Steps for each live bot

1. **Set Fly secrets** for the bot's environment.

   Bot-specific tuning already lives in `fly.toml [env]` with
   `STOCK_` / `CRYPTO_` prefixes; the scheduler auto-strips the prefix
   for the duration of each bot's job (see `_bot_env_scope` and
   `_BOT_ENV_PREFIX` in `agent_runner/scheduler.py`). Fly SECRETS follow
   the same prefix convention for anything sensitive that can't be
   committed to the toml.

   **Bot-specific secrets** (`fly secrets set ...`):

   For `stock_momentum_v1`:
   - `STOCK_SUPABASE_URL` — the stock bot's OWN Supabase project.
   - `STOCK_SUPABASE_SERVICE_KEY` — its service-role key.
   - `POLYGON_API_KEY` — for stock prices. Unprefixed because crypto
     doesn't use it, so no collision risk.

   For `crypto_ema_atr_v1`:
   - `CRYPTO_SUPABASE_URL` — the crypto bot's OWN Supabase project.
   - `CRYPTO_SUPABASE_KEY` — its service-role key. Note: the crypto
     bot's code reads a single `SUPABASE_KEY` (not `SUPABASE_ANON_KEY`
     / `SUPABASE_SERVICE_KEY`); use whichever role you had in the
     original GHA `SUPABASE_KEY` secret.

   **Shared secrets** (unprefixed — one value both bots see):
   - `DISCORD_WEBHOOK_URL` — same webhook for both bots' notifications
     (jerry confirmed 2026-06-01). If you also want the paper bots'
     League-level notifications to go here, set
     `LEAGUE_DISCORD_WEBHOOK_URL` to the same value.
   - `PUBLIC_SECRET`, `PUBLIC_ACCOUNT_ID`, `LEAGUE_SUPABASE_URL`,
     `LEAGUE_SUPABASE_KEY` — already set on Fly for the ETF and other
     paper bots.

   **One-liner:**

   ```powershell
   fly secrets set `
     -a trading-bot-league-agent-runner `
     STOCK_SUPABASE_URL='https://<stock-project>.supabase.co' `
     STOCK_SUPABASE_SERVICE_KEY='<stock-service-role-key>' `
     POLYGON_API_KEY='<polygon-key>' `
     CRYPTO_SUPABASE_URL='https://<crypto-project>.supabase.co' `
     CRYPTO_SUPABASE_KEY='<crypto-supabase-key>' `
     DISCORD_WEBHOOK_URL='<discord-webhook-url>'
   ```

   Setting secrets triggers a machine restart automatically.

2. **Sanity-run once locally** with those env vars exported to confirm
   the bot boots cleanly against real credentials:
   ```powershell
   $env:LEAGUE_BOT_ID = "stock_momentum_v1"; python -m bots.stock_momentum_v1.main
   ```
   You should see a full run: bars fetched, positions read, no orders
   placed if the strategy didn't fire, order placement path exercised
   if it did.

3. **Deploy the code to Fly** WITHOUT setting `LIVE_BOTS_ENABLED`. The
   startup banner should show `LIVE_BOTS_ENABLED not set — ... vendored
   but NOT scheduled on Fly`. This is the safe intermediate state — Fly
   has the code but isn't running it.

4. **Disable the GHA cron** for the bot you're cutting over. In the
   original repo, comment out the `schedule:` block in the workflow YAML
   (keep `workflow_dispatch:` so you can still trigger runs manually).
   Push to the default branch. Confirm on GitHub that the next scheduled
   run does NOT appear on the Actions tab.

5. **Enable Fly scheduling**:
   ```powershell
   fly secrets set LIVE_BOTS_ENABLED=1 -a trading-bot-league-agent-runner
   ```
   Fly restarts the machine automatically after setting secrets. Watch
   the startup banner:
   ```powershell
   fly logs -a trading-bot-league-agent-runner | Select-String "SCHEDULED"
   ```
   You should see `LIVE_BOTS_ENABLED — stock_momentum_v1 and
   crypto_ema_atr_v1 SCHEDULED`.

6. **Watch the first cycle on Fly**. For stock the next :17 on a weekday
   in 14-20 UTC. For crypto the next :07/:22/:37/:52. Tail logs:
   ```powershell
   fly logs -a trading-bot-league-agent-runner | Select-String "stock_momentum_v1|crypto_ema_atr_v1"
   ```
   Confirm one cycle lands cleanly (no ImportError, no auth failures,
   `bot_runs` row shows `success` in Supabase).

7. **Watch for a week.** If clean, delete the GHA workflow file entirely
   from the original repo. If not clean, unset `LIVE_BOTS_ENABLED` (Fly
   restarts, bot stops on Fly), re-enable the GHA cron by uncommenting
   the `schedule:` block, and debug on the Fly side without live-capital
   pressure.

### Order-placement risk gate (deferred)

The vendored bots still enforce risk via their own internal
`MAX_ORDER_AMOUNT_USD`, `DAILY_LOSS_LIMIT`, etc. — same as they did on
GHA. Wiring `league_core.risk.preflight()` into their order-placement
paths as an ADDITIONAL centralized gate is a separate PR after cutover
is stable. The bots' own limits still apply during cutover, so the
existing safety envelope is preserved.

## Retiring the corresponding GHA workflows

After the agent_runner has been live and you've seen at least one full
cycle of each job land cleanly in Supabase, disable (don't delete yet —
keep them for rollback) the GHA workflows:

```
Trading Bot League/.github/workflows/bond_research_v1.yml
Trading Bot League/.github/workflows/options_alert_v1.yml
Trading Bot League/.github/workflows/agent_research_v1.yml
Trading Bot League/.github/workflows/etf_rotation_v1.yml
Trading Bot League/.github/workflows/short_watchlist_v1.yml
Trading Bot League/.github/workflows/league_health.yml
```

To disable without deleting: in each YAML, comment out the `schedule:`
block. `workflow_dispatch:` stays so you can still trigger them manually
for comparison. Once you trust the agent_runner (a few weeks), delete
them entirely.

The `stock_momentum_v1` and `crypto_ema_atr_v1` workflows are NOT
touched.

## Cost & resource notes

- VM: shared-cpu-1x, 256MB RAM. Idle at ~80MB, peaks ~150MB during a
  bot cycle. Plenty of headroom.
- Cost: Fly's pay-as-you-go puts an always-on 256MB shared-cpu-1x at
  roughly **$1.94–$2.02/month**. Fly removed their permanent free
  tier in October 2024; new accounts get a **one-time $5 trial
  credit** (not a recurring monthly one), so the first ~2.5 months
  are effectively free, then it's ~$2/month ongoing.
- Network egress: minimal — Supabase + Public API + Anthropic API
  calls. Pennies/month at our volume.
- LLM costs (the agent_research_v1 daily Claude call) are tracked
  separately on the Anthropic side. Haiku at this context size is
  roughly $0.003/run = ~$1/year.
- **Total to budget**: ~$25/year ongoing after year 1. Set a Fly
  billing alert at $5/month so a stuck restart loop or bandwidth
  spike pings you early.

## Failure modes

- **Container restart**: scheduler picks up immediately, jobs fire on
  their next scheduled time. No backfill of missed runs (coalesce=True
  on the job defaults).
- **Single bot crash**: caught in `_run_bot()`, logged, scheduler keeps
  running. The bot's own `monitor.end_run()` in its `finally:` block
  marks the bot_runs row as failed.
- **All bots crash**: scheduler itself stays up; just won't write
  anything to Supabase. The `league_health` watcher will eventually
  notice the staleness and Discord-ping you.
- **Fly.io platform outage**: nothing fires. Stock and crypto bots
  unaffected (on GHA).
- **Anthropic outage**: only agent_research_v1 affected. Other bots
  proceed normally.
- **Supabase outage**: every bot logs the failure to stdout and exits
  cleanly. Once Supabase recovers, the next scheduled cycle works.

## What's intentionally NOT here yet

- **Live execution paths** for paper bots — Phase 3. Per-bot decision,
  with tiny caps. The risk gate (`league_core/risk.py`) and the equity
  order client (`league_core/public_api/equities.py`) both landed
  2026-05-28; their smoke tests run with
  `python -m league_core._risk_smoke` and
  `python -m league_core._equities_smoke`. The remaining piece is
  wiring `etf_rotation_v1` itself to call `risk.preflight()` +
  `equities.place_market_buy/sell` when `mode='live'`, plus flipping
  its `bot_registry` row to live with tight caps.
- **HTTP `/healthz` endpoint** — could be useful for Fly's checks but
  not required. The scheduler not exiting IS the health signal; Fly
  restarts the machine if the process dies.

Those are explicitly separate approvals, not part of this phase.
