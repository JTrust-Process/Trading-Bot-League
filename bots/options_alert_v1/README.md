# options_alert_v1

**Research-only.** Never trades, never simulates trades, never imports an
order endpoint. Publishes options-strategy ideas to `bot_signals` so a
human (or a future agent bot) can decide whether to act on any of them.

## What it does

Each cycle, for every symbol in the universe:

1. Fetches daily bars from Public.
2. Derives **trend regime** (`bull` / `bear` / `mixed`) from close vs SMA50 / SMA200.
3. Derives **volatility regime** (`low_vol` / `mid_vol` / `high_vol`) by
   comparing realized vol over the last 21 days to a ~10-month baseline
   (200 trading days — comfortably under Public's `YEAR` return of ~252 bars).
4. Maps the (trend × vol) combination to a single recommended strategy
   family from a 3 × 3 matrix.
5. Writes one `options_idea` signal per symbol with rationale, confidence,
   and full metadata (regime breakdown + raw vol numbers).

A summary `OPTIONS_SCAN` event is emitted once per cycle.

## Strategy matrix

| | low_vol (cheap premium) | mid_vol (neutral) | high_vol (rich premium) |
|---|---|---|---|
| **bull**  | covered_call           | bull_put_spread   | long_call_spread |
| **bear**  | iron_condor_skewed_down | bear_call_spread  | long_put_spread  |
| **mixed** | iron_condor             | calendar_spread   | long_strangle    |

All suggestions are **defined-risk** strategies. The bot never suggests
naked calls or naked puts. Even in the most bullish regime we suggest
covered calls (which require owning shares), not naked short puts.

## Universe

```
SPY, QQQ, IWM           # broad / sector ETFs
AAPL, NVDA, TSLA        # high-volume single-name options
```

Override at runtime with `OPTIONS_SYMBOLS=COMMA,LIST`.

## Confidence

Per idea, in [0.4, 1.0]. Composed from:

- **Trend strength**: how far the close is from SMA50 + SMA200 (deeper = stronger).
- **Vol clarity**: how far the realized/baseline ratio is from the bucket
  boundaries (extreme low/high vol = high confidence; borderline mid-vol
  = lower confidence).

A confidence of 0.4 means "a regime call we're barely making"; 1.0 means
"both signals are screaming the same direction."

## Limitations of v1

- **No real options chain data.** We don't fetch strikes, expirations,
  bid/ask, IV, OI, or Greeks. The strategy suggestion is at the family
  level only — `covered_call` rather than "sell the SPY 30-day 5%-OTM call".
- **No specific contract scoring.** A v2 should integrate Public's options
  endpoint (or yfinance as a fallback) to score specific contracts within
  each suggested strategy family.
- **No backtest.** The matrix is rules-of-thumb based on standard option-
  selling literature, not validated on historical chain data.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PUBLIC_SECRET`        |       | Public.com API secret (read-only bars). |
| `LEAGUE_SUPABASE_URL`  |       | League Supabase URL. |
| `LEAGUE_SUPABASE_KEY`  |       | League service-role key. |
| `LEAGUE_BOT_ID`        |       | Must equal `options_alert_v1`. |
| `OPTIONS_BARS_PERIOD`  | `YEAR` | Bars window from Public. |
| `OPTIONS_SYMBOLS`      |       | Optional comma-separated universe override. |

## Schedule

Once per day, `cron: "37 14 * * 1-5"` (10:37 ET during EDT). Daily-bar
inputs only refresh once per session, so a single midday run is enough.
Offset minute :37 to avoid colliding with anything else.

## What it does NOT do

- Never imports `league_core.public_api.*` or any order client.
- Never writes to `bot_trades` or `bot_positions`.
- All signals carry `approval_required=true`, so the future approval
  queue will hold them for human review before any execution bot
  considers acting on them.
- Cannot be promoted to live. Live options trading would need a separate
  `options_paper_v1` → `options_v1` pair, built on top of real chain data.
