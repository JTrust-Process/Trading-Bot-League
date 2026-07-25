# Session summary — 2026-07-24

Full-session record of the audit + cleanup pass covering short_watchlist_v1
(fixed), options_alert_v1 (deprecated), and the broader repo (6 duplicate
GHA workflows disabled). Save this file for reference and delete or move
it to `docs/` when convenient — it is not consumed by any bot.

---

## Phase 1 — short_watchlist_v1 code fix

**Problem:** paper performance was -$8.70 across 6 closed trades (17% win
rate, avg -1.45% return) over 64 days. Diagnosis: mechanics correct
(stops fire, take-profit works), but the entry filter was too permissive
for the persistent bull regime — no regime gate, no min-confidence
threshold, universe of momentum leaders (AAPL/MSFT/NVDA/AMZN/GOOGL/META
/TSLA/QQQ/SPY/IWM/XLK).

**Fix:** three env-var-gated entry filters added to
`bots/short_watchlist_v1/main.py`. Exit logic byte-identical to before.
All changes paper-only.

| Env var | Default | Effect |
|---|---|---|
| `SHORT_REGIME_GATE` | `true` | No new shorts while SPY > SPY SMA(SHORT_REGIME_SMA) |
| `SHORT_REGIME_SMA` | `200` | SMA period for the regime gate |
| `SHORT_MIN_CONFIDENCE` | `0.70` | Filter out low-confidence setups (screener range is 0.5-1.0) |
| `SHORT_REL_WEAKNESS_PCT` | `0.05` | Require symbol's 3m return worse than SPY's by 5%+ |

Would have prevented all 5 losing entries in the paper history.

**Do NOT promote to live** until paper performance improves under bear or
mixed regime OR at least 30 more closed trades pass under the new gates.

## Phase 2 — options_alert_v1 deprecation

**Problem:** bot produces family-level suggestions (e.g., `bull_put_spread`)
with no strikes/expirations/greeks. 606 signals in 72 days growing
`bot_signals` daily with no downstream execution consumer. Design
predates Public's real options API (chains, greeks, multi-leg place,
strategy-quote — added Feb-May 2026).

**Action:** deprecated. Scheduler job commented out in
`agent_runner/scheduler.py`. README banner added at top of
`bots/options_alert_v1/README.md`. Bot code kept for reference / revival.

**SQL still to run:**
```sql
update public.bot_registry
set status = 'disabled', updated_at = now()
where bot_id = 'options_alert_v1';
```

**Future replacement:** `options_paper_v1` (not built) would use Public's
new options endpoints to select actual contracts, use `strategy-quote`
for pricing, simulate paper fills, track P/L via daily mark polling.
Scope as its own 3-4 session project when appetite exists.

## Phase 3 — broader cleanup

**Big find: 6 GHA workflows in the League repo were still firing crons at
the same times as their Fly-scheduled equivalents.** Every paper/research
bot had been running BOTH on GHA AND Fly since the Fly cutover, doubling
DB writes. For `agent_research_v1` this also meant 2x Anthropic API cost
(small dollar amount but real). For `options_alert_v1` GHA was still
firing the bot we JUST disabled on Fly — undoing our deprecation work.

**Action:** all six workflow `schedule:` blocks commented out. Same
pattern as the stock/crypto GHA disables. `workflow_dispatch:` kept for
emergency manual runs.

| Workflow | Old cron | New state |
|---|---|---|
| `.github/workflows/etf_rotation_v1.yml` | `33 14-20 * * 1-5` | schedule commented out |
| `.github/workflows/bond_research_v1.yml` | `35 14 * * 1-5` | schedule commented out |
| `.github/workflows/short_watchlist_v1.yml` | `41 14-20 * * 1-5` | schedule commented out |
| `.github/workflows/options_alert_v1.yml` | `43 14 * * 1-5` | schedule commented out |
| `.github/workflows/agent_research_v1.yml` | `50 14 * * 1-5` | schedule commented out |
| `.github/workflows/league_health.yml` | `9,24,39,54 * * * *` | schedule commented out |

## Files changed this session (uncommitted)

Run `git status` in `C:\Users\Jeremiah\source\Trading Bot League` — you
should see:

Code / config:
- `bots/short_watchlist_v1/main.py` (regime/confidence/rel-weakness gates)
- `bots/options_alert_v1/README.md` (deprecation banner)
- `agent_runner/scheduler.py` (options_alert_v1 job commented out)
- `agent_runner/README.md` (job table updated)

GHA workflows (all 6 schedule blocks commented out):
- `.github/workflows/etf_rotation_v1.yml`
- `.github/workflows/bond_research_v1.yml`
- `.github/workflows/short_watchlist_v1.yml`
- `.github/workflows/options_alert_v1.yml`
- `.github/workflows/agent_research_v1.yml`
- `.github/workflows/league_health.yml`

Seed SQL (make the DB defaults match reality):
- `supabase/seed/options_alert_v1_seed.sql` — default `status='disabled'`
  and DEPRECATED notes; so re-running the seed won't re-enable the bot
- `supabase/seed/bot_expenses_seed.sql` — Claude Pro attribution
  corrected from "AI trading via MCP server" (fiction) to "dev/chat
  sessions" (reality)

Docs:
- `PLAN.md` — status header updated. Original opener said "no code
  changes yet"; now marks the plan as largely implemented with a
  quick summary of what shipped, deprecated, and remains open
- `SESSION_2026-07-24_SUMMARY.md` (this file — safe to delete after
  reading, or move to `docs/` if you want to keep it)

## Deploy checklist

Run in order:

```powershell
cd "C:\Users\Jeremiah\source\Trading Bot League"
git add -A
git commit -m "Session 2026-07-24: short_watchlist gates + options_alert deprecated + disable 6 duplicate GHA workflows"
git push
fly deploy -a trading-bot-league-agent-runner
```

Then run the SQL from Phase 2 in the League Supabase SQL editor.

**Expected startup banner after deploy:** 7 jobs scheduled
(bond_research, agent_research, etf_rotation, short_watchlist,
stock_momentum, crypto_ema_atr, league_health) — same 7 as before,
NOT 8 (options_alert dropped). LIVE_BOTS_ENABLED still shows enabled.

## What's still open / not-done

- **~~`bot_expenses_seed.sql` Claude Pro line~~** — DONE this session.
  Attribution corrected in the seed. If you want the row in Supabase
  to reflect the new note immediately, re-run the seed or run:
  ```sql
  update public.bot_expenses
  set note = 'Claude Pro — dev/chat sessions for building and maintaining '
             || 'the League (code review, SQL, docs, manual ops). No MCP '
             || 'server in play as of 2026-07-24. Update if subscription '
             || 'tier or usage attribution changes.'
  where bot_id is null
    and category = 'claude_subscription'
    and period = '2026-05';
  ```
- **`PUBLIC_ACCOUNT_ID_ACCOUNT2` Fly secret** — leftover from the
  shadow-logger / account-2 era. Nothing reads it. Housekeep with:
  ```powershell
  fly secrets unset PUBLIC_ACCOUNT_ID_ACCOUNT2 -a trading-bot-league-agent-runner
  ```
- **Dashboard "degraded" false positives** for low-cadence bots — the
  staleness threshold doesn't know each bot's cadence, so bots that run
  once a day (bond_research, agent_research) get flagged as degraded
  for most of the day. Dashboard-side fix, separate project.
- **~~`options_alert_v1_seed.sql`~~** — DONE this session.
- **Cheap safety wins on existing live bots** (add `useMargin: false`,
  integrate preflight endpoint) — deferred; small drop-in work.
- **~~PLAN.md staleness~~** — DONE this session. Status header now
  says "LARGELY IMPLEMENTED as of 2026-07-24" with a brief summary of
  what shipped vs the original plan.
- **Cross-bot code duplication** — `_env_float`, `_env_bool`,
  `_classify` are duplicated across bots. Could factor into
  `league_core/config.py`. Non-urgent.
- **`options_paper_v1`** — the real replacement for the deprecated
  options_alert. Multi-session project. Scope separately.

## Memory updates

New: `paper-research-bots-audit-2026-07.md` (indexed in `MEMORY.md`).
Captures all findings + forward direction for the four
paper/research bots.

Updated: previous memories are still accurate; no edits needed.

## Key numbers from this session

- `short_watchlist_v1` paper history: **9 shorts, 6 covers, 1 winner
  (+12.95% TSLA), 5 losers (avg -4.3%), -$8.70 total PnL, 17% win rate**
- `options_alert_v1`: **606 signals, 6 symbols, avg confidence 0.78,
  72 days of daily fires — zero downstream execution**
- `bond_research_v1`: **847 scores, 8 bonds (BND/TIP/HYG/LQD/TLT/IEF/
  SHY/SGOV), still scoring daily**
- `etf_rotation_v1`: **0 live trades since going live 2026-06 — SPY has
  held above 50-SMA the whole time, no regime change to trigger a
  rebalance. Truly live, truly dormant, correctly designed.**
- Fly cutover for stock + crypto: **completed 2026-07-24; first live
  Fly cycles crypto 07-25 00:52 UTC, stock 07-27 14:17 UTC**
