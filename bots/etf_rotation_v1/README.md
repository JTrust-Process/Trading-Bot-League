# etf_rotation_v1

**Paper-only** ETF rotation bot. Never places real orders.

## Strategy

```
Regime:  SPY close > SPY 50-day SMA   →  bull
         otherwise                     →  bear

Target:  bull  →  SPY 25%, QQQ 25%, VTI 25%, SCHD 25%, SGOV 0%
         bear  →  100% SGOV (cash-like)

Trigger: any change in target set vs. last_target_set in state.json.
         On change, close everything currently held and open the new
         target set, each weighted equally with `ETF_PAPER_CAPITAL / N`.
```

That's the entire strategy. Intentionally tiny.

## What it writes (all in the League Supabase project)

- `bot_runs`            — one row per cycle (via `league_status.start_run` / `end_run`).
- `bot_status`          — single row keyed by `bot_id`, upserted.
- `bot_events`          — REGIME_CHECK every cycle, REGIME_CHANGE on rebalance.
- `bot_trades`          — one row per simulated BUY/SELL, `is_paper=True`, `asset_class='etf'`.
- `bot_positions`       — open / closed paper positions. Unique partial index on `(bot_id, symbol)` where `status='open'` keeps the table consistent.

## What it never writes

- Real Public orders. The order endpoint is not imported anywhere in this bot.
- The Stock or Crypto bot's existing Supabase projects.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PUBLIC_SECRET`            |  | Public.com API secret. Required to fetch bars. |
| `LEAGUE_SUPABASE_URL`      |  | League Supabase project URL. |
| `LEAGUE_SUPABASE_KEY`      |  | League **service-role** key (writes need RLS bypass). |
| `LEAGUE_BOT_ID`            |  | Must equal `etf_rotation_v1` so writes attribute correctly. |
| `ETF_PAPER_CAPITAL`        | `1000` | Paper starting capital ($). Used only to size new positions on each rebalance. |
| `ETF_BARS_PERIOD`          | `YEAR` | Period passed to Public's bars endpoint. `YEAR` ≈ 252 daily bars — enough for SMA(50). |

## Schedule

Hourly during US market hours, Monday-Friday — same cadence as the live
stock bot. See `.github/workflows/etf_rotation_v1.yml`.

The bot only acts on a regime change. On a typical day where SPY stays
on one side of its SMA, every cycle is a no-op rebalance that simply
heartbeats and exits.

## State

`state.json` is persisted via GHA cache, same pattern as the crypto bot.
It holds only:

- `last_target_set`: list of symbols we were trying to hold last cycle.
- `last_rebalance_at`: ISO timestamp for visibility.
- `paper_capital`: starting capital in dollars.

If the cache is lost, the next run treats the empty state as "no
positions yet" and opens the current regime's full target set. Real
PnL on those re-opens may be artificial, but the bot self-heals.

## Local smoke test

```bash
# from the Trading Bot League repo root
cd bots/etf_rotation_v1
python -m venv .venv && source .venv/Scripts/activate   # Windows
pip install -r requirements.txt

# put PUBLIC_SECRET + LEAGUE_SUPABASE_URL + LEAGUE_SUPABASE_KEY +
# LEAGUE_BOT_ID=etf_rotation_v1 in .env (or export them in your shell),
# then run from the repo root so the relative imports resolve:
cd ../..
python -m bots.etf_rotation_v1.main
```

You'll see `[etf]` and `[league]` log lines. Check the League Supabase
project — `bot_runs`, `bot_status`, `bot_events`, and (on a regime
change) `bot_trades` + `bot_positions` should populate.

## Disabling / killing

The League risk envelope already covers this. Either:

```sql
update bot_registry set status = 'disabled' where bot_id = 'etf_rotation_v1';
-- or, harsher:
update bot_registry set status = 'killed' where bot_id = 'etf_rotation_v1';
```

The bot itself doesn't yet read `bot_registry.status` (Stage 5 is just the
first paper bot; the preflight integration is Stage 6). For now, disable
via the GHA workflow toggle in GitHub's UI, or by deleting the workflow
trigger.
