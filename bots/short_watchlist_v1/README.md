# short_watchlist_v1

**Paper-only.** Detects bearish setups, publishes them as `SHORT` signals,
and simulates paper short trades. Never places a live short order — there
is no order code path in this bot.

## What it does

Each cycle, for every symbol in the universe:

1. Fetch daily bars from Public.
2. If we already hold an **open paper short** for the symbol, run the
   exit detector. On any exit trigger, write a `short_exit` signal,
   record a `COVER` paper trade with PnL, and close the position.
3. Otherwise, run the entry detector. On a fresh bearish setup, write a
   `short_setup` signal, record a `SHORT` paper trade, and open a paper
   position with `metadata.direction = 'short'`.

A summary `SHORT_WATCH_SURVEY` event is emitted once per cycle.

## Strategy

**Entry** — all rules must hold:

| Rule | Default |
|---|---|
| Close < SMA50 | confirmed downtrend |
| Close < SMA200 | long-term downtrend |
| Close <= 20-day rolling low | breakdown |
| 3-month return <= -5% | negative momentum |

**Exit** — any rule triggers a `COVER`:

| Rule | Reason code |
|---|---|
| Close > SMA20 | `trend_reversal` |
| Adverse move >= 5% from entry (price up) | `stop` |
| Favorable move >= 10% from entry (price down) | `take_profit` |

PnL convention for shorts (matches the bot's internal math):

```
pnl_usd = (entry_price - exit_price) * quantity
pnl_pct = (entry_price - exit_price) / entry_price
```

Positive PnL means price fell after entry. A 10% drop is `pnl_pct = +0.10`.

## Universe

```
AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA,   # high-beta equities
QQQ, SPY, IWM, XLK                            # index/sector ETFs
```

Override at runtime with `SHORT_SYMBOLS=COMMA,LIST`.

## Sizing

Each open short is sized at `SHORT_CAPITAL_PER_TRADE` USD (default `100`).
Quantity is computed as `capital / entry_price`. No position scaling, no
volatility-based sizing — kept simple for v1.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PUBLIC_SECRET`            |       | Public.com API secret (read-only bars). |
| `LEAGUE_SUPABASE_URL`      |       | League Supabase URL. |
| `LEAGUE_SUPABASE_KEY`      |       | League service-role key (writes). |
| `LEAGUE_BOT_ID`            |       | Must equal `short_watchlist_v1`. |
| `SHORT_CAPITAL_PER_TRADE`  | `100` | Paper $ per simulated short. |
| `SHORT_BARS_PERIOD`        | `YEAR` | Bars window passed to Public. |
| `SHORT_SYMBOLS`            |       | Optional comma-separated universe override. |

## Schedule

Hourly market-hours, `cron: "41 14-20 * * 1-5"` (10:41 ET during EDT).
Same cadence as the stock bot, offset to minute :41 to avoid colliding
with the other crons.

## What it does NOT do

- Never imports `league_core.public_api.*` or any order client.
- Never sets `is_paper=False` on any trade or position row.
- Cannot be promoted to live in place. If you want a live short bot, build
  a separate `short_paper_v1` → `short_v1` pair.

## Where it shows up on the dashboard

- **Bots** rail: under "Paper" alongside `etf_rotation_v1`.
- **Recent trades**: SHORT and COVER rows tagged paper.
- **Research scores**: not applicable to this bot.
- **Signals** (when that section ships): `short_setup` and `short_exit`.
