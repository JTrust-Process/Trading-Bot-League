# etf_rotation_v1

ETF rotation bot. Mode-aware: when `bot_registry.mode = 'paper'` (the
default) it simulates fills exactly as before; when `mode = 'live'` it
places real Public.com market orders via `league_core.public_api.equities`
after going through the `league_core.risk` preflight gate.

`mode` is read from Supabase at the start of every cycle, so flipping
paper ↔ live is a SQL update (no redeploy required). On any registry
lookup failure the bot defaults to `paper` — the safe fallback.

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

- `bot_runs`        — one row per cycle.
- `bot_status`      — single row keyed by `bot_id`, upserted.
- `bot_events`      — `REGIME_CHECK` every cycle, `REGIME_CHANGE` on rebalance.
                       In live mode, also `RISK_REFUSED` / `ORDER_FAILED` /
                       `PRICE_UNAVAILABLE` / `NO_PRIOR_POSITION` when the
                       respective edge cases fire.
- `bot_trades`      — one row per BUY/SELL. `asset_class='etf'`.
                       In paper mode: `is_paper=True`.
                       In live mode: `is_paper=False`, `order_id` set to the
                       deterministic Public order id, `metadata.dry_run`
                       flagged when `PUBLIC_DRY_RUN=1`,
                       `metadata.fill_price_estimated=true` when fill
                       discovery fell back to the bar close.
- `bot_positions`   — open / closed positions. Same partial unique index on
                       `(bot_id, symbol)` where `status='open'` regardless
                       of mode.

## What it never writes

- The Stock or Crypto bot's existing Supabase projects (they live in
  separate Supabase projects altogether).
- Real Public orders WHEN `mode='paper'`. The live path is taken only
  when `bot_registry.mode='live'` for this `bot_id`. The risk gate
  (`league_core.risk.preflight`) is the architectural guarantee — even
  if the order client were called by mistake, preflight would refuse
  with `REASON_MODE_NOT_LIVE`.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PUBLIC_SECRET`            |  | Public.com API secret. Required to fetch bars AND to place live orders. |
| `PUBLIC_ACCOUNT_ID`        |  | Optional in paper mode. **Required** in live mode — explicit pin of which Public account to trade against. Without it, the auth helper would fall back to the first account, which is risky if you have more than one. |
| `LEAGUE_SUPABASE_URL`      |  | League Supabase project URL. |
| `LEAGUE_SUPABASE_KEY`      |  | League **service-role** key (writes need RLS bypass). |
| `LEAGUE_BOT_ID`            |  | Must equal `etf_rotation_v1` so writes attribute correctly. |
| `ETF_PAPER_CAPITAL`        | `1000` | Per-cycle total notional ($). Despite the name (kept for back-compat with paper-only history), this is the sizing reference in BOTH modes: each new position gets `ETF_PAPER_CAPITAL / N` of capital. Set low (e.g. `100`) for first live cycles. |
| `ETF_BARS_PERIOD`          | `YEAR` | Period passed to Public's bars endpoint. `YEAR` ≈ 252 daily bars — enough for SMA(50). |
| `PUBLIC_DRY_RUN`           |  | Optional. When `1` / `true`, the order client returns synthetic success without calling Public. Recommended for the FIRST live cycle: set this, watch the cycle log the would-be payloads, then unset it for actual order placement. |
| `LEAGUE_KILL`              |  | Global kill switch. When `1` / `true`, every `risk.preflight()` call refuses with `REASON_KILL_GLOBAL`. Flippable on Fly via `fly secrets set LEAGUE_KILL=1`. |

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

Three escalating options, from soft to hard:

```sql
-- 1. Move back to paper. The bot keeps running but takes the simulated
--    path. Live orders stop instantly on the next cycle.
update bot_registry set mode = 'paper' where bot_id = 'etf_rotation_v1';

-- 2. Disable the bot entirely. risk.preflight refuses every order with
--    REASON_KILL_BOT. The bot's cycle still runs (so health stays fresh)
--    but it can't place orders even if mode says 'live'.
update bot_registry set status = 'disabled' where bot_id = 'etf_rotation_v1';

-- 3. Kill switch — affects EVERY bot in the league, not just this one.
--    Useful if something looks really wrong across the system.
--    Reverse with `fly secrets unset LEAGUE_KILL`.
```

```bash
fly secrets set LEAGUE_KILL=1
```

Live order placement now goes through `risk.preflight()`, which checks
all three of the above. Refusals are logged to `bot_events` with
`event_type='RISK_REFUSED'` and the specific reason code.
