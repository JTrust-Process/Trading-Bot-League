# bond_research_v1

**Research-only**. Never trades, never simulates trades, never imports an
order endpoint.

## What it does

Once per day during a market-hours window, the bot:

1. Fetches daily bars for a fixed universe of bond ETFs from Public.
2. Scores each on a four-component composite (trend, momentum, stability, liquidity).
3. Classifies each into `keep_active` / `reduce_priority` / `paper_only` / `remove`.
4. Writes one row per symbol into `bot_research_scores` in the League Supabase project.
5. Emits one `BOND_SCREENED` event in `bot_events` summarizing the cycle.

Humans read the output on the `/league` dashboard. A future
`bond_paper_v1` bot may consume the scores as its idea source; this bot
itself does not consume them and does not act on them.

## Universe

Hand-curated bond-ETF cross-section:

| Symbol | Description |
|---|---|
| SGOV | 0–3 month T-bills (cash proxy) |
| SHY  | 1–3 yr treasuries |
| IEF  | 7–10 yr treasuries |
| TLT  | 20+ yr treasuries |
| LQD  | Investment-grade corporate |
| HYG  | High-yield corporate |
| TIP  | Inflation-protected treasuries |
| BND  | Total bond market |

Override at runtime via `BOND_SYMBOLS=COMMA,SEPARATED,LIST` in the env.

## Scoring

Composite ∈ [0, 1] weighted from four components when available:

| Component  | Weight | Definition |
|---|---|---|
| Trend      | 0.35 | 1 if close > SMA200 else 0 |
| Momentum   | 0.30 | 3-month return / 10%, clipped to [0, 1] |
| Stability  | 0.20 | 1 − (annualized vol / 15%), clipped to [0, 1] |
| Liquidity  | 0.15 | 1 if 20-day avg vol > 1M else 0.5 |

If a component can't be computed (e.g. fewer than 200 bars for SMA200),
its weight is redistributed across the remaining components. So we never
penalize a symbol for missing data — we just score with less confidence.

Bucket cuts on the composite:

| Composite | Bucket |
|---|---|
| ≥ 0.70 | `keep_active`     (strong) |
| 0.45 – 0.70 | `reduce_priority` (decent) |
| 0.25 – 0.45 | `paper_only`      (marginal) |
| < 0.25 | `remove`          (weak) |

Tune the weights and cuts in `screener.py` — none of them are sacred.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `PUBLIC_SECRET`        |        | Public.com API secret. Required to fetch bars. |
| `LEAGUE_SUPABASE_URL`  |        | League Supabase project URL. |
| `LEAGUE_SUPABASE_KEY`  |        | League service-role key. |
| `LEAGUE_BOT_ID`        |        | Must equal `bond_research_v1`. |
| `BOND_BARS_PERIOD`     | `YEAR` | Period passed to Public's bars endpoint. |
| `BOND_SYMBOLS`         |        | Optional comma-separated override of the universe. |

## Schedule

Once per day at 14:35 UTC (10:35 ET during EDT). The score doesn't change
intraday in a useful way — daily-bar inputs only refresh daily.

## What it does NOT do

- Never imports `league_core.public_api.*` or any order client.
- Never writes to `bot_trades` or `bot_positions`.
- Never reads `bot_signals` or `bot_approvals`.
- Cannot be promoted to live trading. If you want a live bond bot, build
  a separate `bond_paper_v1` → `bond_v1` pair. This bot is permanently
  a screener.
