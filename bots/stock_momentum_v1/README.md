# stock_momentum_v1

Live-capital stock momentum / breakout bot. Vendored into the Trading
Bot League repo on 2026-06-01 as part of the GHA → Fly migration. The
original source still lives at
`C:\Users\Jeremiah\source\Trading Bot\Trading Bot Project\` as a
historical archive — **all forward-going changes must happen in this
copy**, not the original.

## Migration status

This is a vendor copy only. As of 2026-06-01:

- [x] Source files copied into this directory.
- [x] `main.py` wrapper added — exposes `run_cycle()` for
      `agent_runner.scheduler` to call.
- [ ] Job added to `agent_runner/scheduler.py` (next PR).
- [ ] Fly secrets set for this bot's env vars (next PR).
- [ ] Order-placement paths in `bot.py` wrapped in
      `league_core.risk.preflight()` for centralized risk gating.
- [ ] GHA workflow `trading-bot.yml` disabled in original repo.
- [ ] Original GHA workflow deleted (after ~1 week of clean Fly cycles).

Until the GHA workflow is disabled in the original repo, **the bot runs
in two places**. Don't enable the Fly job until the GHA cron is paused
to avoid double-firing the live cycle. The deterministic-uuid pattern
in `bot.deterministic_order_id` dedupes within the same minute on
Public's side, but cross-minute double-fires would result in duplicate
orders.

## Files

### Live runtime (vendored from original)

| File | Role |
|---|---|
| `bot.py`           | Main entry. `def main()` runs one cycle. Has order-placement, risk checks, regime detection. |
| `monitor.py`       | `Monitor` class — lifecycle / Supabase logging for the bot's own Supabase project (NOT the League's). |
| `notify.py`        | Discord webhook embeds. |
| `strategy.py`      | ATR + trend-strength + simple SMA cross signal. |
| `momentum.py`      | Momentum-rotation strategy. |
| `breakout.py`      | Breakout signal. |
| `market_data.py`   | Polygon API client for stock prices. |
| `public_bars.py`   | Public.com bars endpoint client (research surface, additive). Separate from `league_core/public_bars.py`. |
| `indicators.py`    | Technical indicators. |
| `league_status.py` | Stage-1b heartbeat adapter to the League's Supabase project. Fail-silent. |
| `scripts/finalize_runs.py` | Closes out `bot_runs` rows that were SIGKILLed mid-cycle. Was a separate GHA step. |
| `requirements.txt` | Bot-specific Python deps (pandas, supabase, yfinance, etc.). |

### Wrapper (new, added in vendoring)

| File | Role |
|---|---|
| `__init__.py`      | Empty package marker. |
| `main.py`          | Adds this directory to sys.path, imports `bot`, exposes `run_cycle()`. |

### NOT vendored

These files exist in the original repo but aren't needed at runtime:

- `backtest.py`, `backtest_public.py`, `analyze_backtests.py` — backtest scaffolding.
- `list_symbols.py` — one-off utility.
- `scripts/list_public_accounts.py` — diagnostic utility.
- `venv/` — local virtualenv, not source.
- `.github/workflows/trading-bot.yml` — the existing GHA workflow stays in the original repo until cutover.

## Environment variables

Same as the existing GHA workflow's `env:` block (see
`Trading Bot/Trading Bot Project/.github/workflows/trading-bot.yml`).
Roughly:

**Secrets (must be set on Fly via `fly secrets set ...`):**
- `PUBLIC_SECRET` — Public.com API secret. Already set on Fly for ETF rotation.
- `PUBLIC_ACCOUNT_ID` — explicit account pin. Already set on Fly.
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` — the **bot's own** Supabase project (different from `LEAGUE_SUPABASE_URL`).
- `POLYGON_API_KEY` — for stock prices.
- `DISCORD_WEBHOOK_URL` — for trade notifications.
- `LEAGUE_SUPABASE_URL`, `LEAGUE_SUPABASE_KEY` — for cross-bot heartbeat / mirror. Already set on Fly.

**Non-secret tuning (can live in `fly.toml [env]` block):**

`MODE=live`, `RUN_ONCE=1`, `LEAGUE_BOT_ID=stock_momentum_v1`,
`SYMBOLS`, `SCAN_SYMBOLS`, `MOMENTUM_*`, `MAX_*`, `TAKE_PROFIT_*`,
`STOP_LOSS_*`, `MIN_HOLD_DAYS`, `REGIME_*`, `ATR_*`, `TREND_*`,
`MAX_DRAWDOWN`, `DAILY_LOSS_LIMIT`, `LOSS_COOLDOWN_DAYS`,
`MAX_EXPOSURE_EARLY`, `BASE_POSITION_SIZE`, `MAX_POSITION_SCALE`,
`TZ=America/New_York`.

The exact values are documented in the original `.github/workflows/trading-bot.yml`
and need to be ported to Fly when the cutover happens.

## Schedule

Currently hourly during US market hours, weekdays only:
`cron: "17 14-20 * * 1-5"` (UTC). The same cron will be reproduced in
`agent_runner/scheduler.py` when the cutover step lands.

## How to run locally

From the **League repo root**:

```bash
# Set the same env vars the GHA workflow sets (see above), then:
python -m bots.stock_momentum_v1.main
```

You should see the bot's own startup output, the same as `python bot.py`
in the original directory.
