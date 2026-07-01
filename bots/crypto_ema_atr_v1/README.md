# crypto_ema_atr_v1

Live-capital crypto EMA + ATR bot. Vendored into the Trading Bot League
repo on 2026-06-01 as part of the GHA → Fly migration. The original
source still lives at
`C:\Users\Jeremiah\source\Crypto_Trading_Project\Crypto_Trading_Bot\` as
a historical archive — **all forward-going changes must happen in this
copy**, not the original.

## Migration status

Vendor phase only. As of 2026-06-01:

- [x] Source files copied into this directory (including the whole
      `crypto_bot/` package tree).
- [x] `main.py` wrapper added — exposes `run_cycle()` via
      `runpy.run_module` around the vendored `_entry.py`.
- [ ] Job added to `agent_runner/scheduler.py` (next PR).
- [ ] Fly secrets set for this bot's env vars (next PR).
- [ ] Order-placement paths in `crypto_bot/execution/trader.py` wrapped
      in `league_core.risk.preflight()` for centralized risk gating.
- [ ] GHA workflow `crypto_bot.yaml` disabled in original repo.
- [ ] Original GHA workflow deleted (after ~1 week of clean Fly cycles).

Until the GHA workflow is disabled, **the bot runs in two places**.
Don't enable the Fly job until the GHA cron is paused, or you'll
double-fire the 15-minute live cycle.

## Layout

Unlike the stock bot (flat scripts), the crypto bot is a proper Python
package. Layout after vendoring:

```
bots/crypto_ema_atr_v1/
├── __init__.py       (empty — package marker for League import path)
├── main.py           (WRAPPER — new; runpy-wraps _entry.py)
├── _entry.py         (RENAMED from original main.py; byte-identical to it)
├── README.md         (this file — new)
├── requirements.txt  (copied)
└── crypto_bot/       (copied recursively — the real package)
    ├── __init__.py
    ├── core/
    │   └── engine.py         # main event loop
    ├── config/
    │   └── settings.py       # env-var config
    ├── data/
    │   ├── market_data.py    # Public bars
    │   └── coingecko.py      # CoinGecko OHLC fallback
    ├── exchange/
    │   └── public_api.py     # Public order client
    ├── execution/
    │   └── trader.py         # BUY / SELL execution
    ├── league/
    │   ├── __init__.py
    │   └── league_status.py  # heartbeat to League Supabase (fail-silent)
    ├── logging/
    │   ├── logger.py         # console formatter
    │   ├── monitor.py        # Monitor class (bot's own Supabase)
    │   ├── _supabase.py      # low-level Supabase client
    │   └── supabase_logger.py # log/event/error inserters
    ├── notifications/
    │   └── discord.py        # webhook embeds
    ├── risk/
    │   └── manager.py        # ATR sizing, DL, cooldowns
    ├── state/
    │   ├── state.py          # local state.json IO
    │   └── remote.py         # Supabase state mirror
    ├── strategy/
    │   └── signal.py         # EMA cross detection
    └── utils/
        └── retry.py          # exponential-backoff helper
```

## Why `main.py` was renamed to `_entry.py`

The vendored file wants to keep its script-style `if __name__ ==
"__main__":` block. If it stayed named `main.py`, the wrapper's own
`main.py` couldn't sit in the same directory. Renaming it (rather than
moving it inside `crypto_bot/`) preserves the original's intent — it's
the module you execute — while giving the wrapper the standard
`bots.crypto_ema_atr_v1.main` module path that `agent_runner.scheduler`
expects.

The wrapper (`main.py`) uses `runpy.run_module("_entry",
run_name="__main__")` to run `_entry.py` as if it were still the script
entry. Zero source modifications to the vendored code.

## Not vendored

- `emaCrossover.py` at the original repo root — legacy monolith from
  before the modular refactor. Not part of the runtime.
- `venv/` — local virtualenv.
- `.github/workflows/crypto_bot.yaml` — the existing GHA workflow stays
  in the original repo until cutover.
- `state.json`, `.env` — runtime state / secrets. Not copied.

## Environment variables

Same set the existing GHA workflow provides. Broadly:

**Secrets (Fly-side, `fly secrets set ...`):**
- `PUBLIC_SECRET`, `PUBLIC_ACCOUNT_ID` — Public.com auth. Already set on Fly.
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY` — the
  crypto bot's **own** Supabase project (NOT the League's).
- `DISCORD_WEBHOOK_URL` — for notifications.
- `LEAGUE_SUPABASE_URL`, `LEAGUE_SUPABASE_KEY` — cross-bot mirror.
  Already set on Fly.

**Non-secret tuning (fly.toml `[env]` or Fly secrets, either works):**

`LEAGUE_BOT_ID=crypto_ema_atr_v1`, `DRY_RUN=0`, `SYMBOLS`, `EMA_FAST`,
`EMA_SLOW`, `ATR_PERIOD`, `MAX_ORDER_AMOUNT_USD`, `DAILY_LOSS_LIMIT`,
`MIN_HOLD_MINUTES`, and whatever else lives in
`crypto_bot/config/settings.py`. Values are documented in the original
`.github/workflows/crypto_bot.yaml`.

## Schedule

24/7, every 15 minutes: `cron: "7,22,37,52 * * * *"` (offset by 7 so
it doesn't collide with the stock bot's `:17`). The same cron will be
reproduced in `agent_runner/scheduler.py` when the cutover step lands.

## How to run locally

From the **League repo root**:

```bash
# Set the same env vars the GHA workflow sets, then:
python -m bots.crypto_ema_atr_v1.main
```

Expect it to fail on missing `PUBLIC_SECRET` or `SUPABASE_URL` etc. if
you don't set them — that's the same sanity-check pattern used for the
stock bot vendor test.
