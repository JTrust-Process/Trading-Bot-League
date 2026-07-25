# Session summary — live bot safety hardening (2026-07-25)

Goal for the session: add pre-trade safety (cash-only enforcement +
Public's preflight validation) to the live order paths.

**Shipped:** the `league_core` equities client — the path
`etf_rotation_v1` uses for live orders.

**Deliberately NOT shipped:** the stock and crypto bot order paths. A
design constraint surfaced mid-session that makes those changes riskier
than they looked; see "Why stock/crypto stopped" below. They are queued
as tasks with the constraint documented.

---

## The `useMargin` finding (important)

Public's changelog dated **2026-06-16** announces:

> Orders now support an optional `useMargin` field on both single-leg and
> multi-leg order requests. When set to `false`, the order is evaluated
> using cash-only buying power instead of margin buying power.

**That field does not appear anywhere in the documented request schema.**
Checked on 2026-07-24:

| Source | `useMargin` present? |
|---|---|
| `POST .../order` request body schema | No |
| `POST .../preflight/single-leg` request body schema | No |
| "Placing your first equity order" official guide | No |
| Order Limits reference page | No |

Sending an undocumented field into a **live-capital order payload** to
achieve a *safety* goal is backwards. If the API rejects unknown fields,
every live order starts failing.

### What we did instead — strictly better

Enforce cash-only **in our own code**, using documented preflight
response fields:

```
POST /userapigateway/trading/{accountId}/preflight/single-leg
  body = order body MINUS orderId (+ optional validateOrder, default true)

response includes:
  orderValue, estimatedCost, estimatedQuantity, estimatedProceeds
  buyingPowerRequirement
  estimatedCommission, regulatoryFees{secFee,tafFee,orfFee,...}
  marginImpact{marginUsageImpact, initialMarginRequirement}
  marginRequirement{longInitialRequirement, longMaintenanceRequirement}
  shortSelling{availability, hardToBorrowPercentageRate, uptickRule, ...}
  priceIncrement{...}
```

Plus `portfolio/v2` → `buyingPower.cashOnlyBuyingPower`.

So: run preflight, refuse the order if it would consume margin or exceed
cash-only buying power. Documented behavior, no unverified fields, and we
get cost/fee estimates as a bonus.

If Public later documents `useMargin`, adding it is a one-line change in
`_build_payload` — noted in the module docstring.

Public's own docs back this approach:
> "Always run preflight checks to understand costs before placing orders."
> "Limits are enforced at order placement — use the preflight endpoint to
> validate an order before submitting."

---

## What shipped: `league_core/public_api/equities.py`

New public surface:

| Function | Purpose |
|---|---|
| `preflight_single_leg(symbol, side, amount_usd=, quantity=)` | Calls Public's preflight. Same structured result shape as the order functions. 401-retry-once like `_post_order`. |
| `get_cash_only_buying_power()` | Reads `buyingPower.cashOnlyBuyingPower` from portfolio v2. `None` on failure = "unknown", not "zero". |
| `_evaluate_preflight(pf, side, cash_only, cash_buying_power)` | **Pure function.** The rules engine. No IO, no env reads — fully unit-testable. |
| `_run_pre_trade_checks(...)` | Orchestrator wired into both order functions. |

New reason codes (stable strings, greppable, same convention as
`league_core.risk`):

```
PRE_OK                = "ok"
PRE_MARGIN_REQUIRED   = "preflight_margin_required"
PRE_INSUFFICIENT_CASH = "preflight_insufficient_cash_buying_power"
PRE_VALIDATION_FAILED = "preflight_validation_failed"
PRE_CALL_FAILED       = "preflight_call_failed"
PRE_NOT_SHORTABLE     = "preflight_not_shortable"
```

### Env flags (all read at call time, no redeploy to change)

| Var | Default | Effect |
|---|---|---|
| `EQUITIES_PREFLIGHT` | `true` | Run preflight before real orders |
| `EQUITIES_CASH_ONLY` | `true` | Refuse orders that would consume margin |
| `EQUITIES_PREFLIGHT_STRICT` | `false` | Also refuse when the preflight *call itself* fails |

### Failure semantics (deliberate)

- **Preflight returns 4xx** → Public evaluated the order and said no.
  Definitive negative. **Always refuse**, regardless of strict mode.
- **Preflight call fails** (network/timeout/5xx) → we couldn't check.
  Default is **allow with a logged warning**, because (a) Public still
  enforces buying power at placement, and (b) `league_core.risk.preflight`
  already gated this order upstream. Set
  `EQUITIES_PREFLIGHT_STRICT=true` to refuse instead.
- **Missing/garbage fields in the response** → never blocks. All numerics
  parse through `_to_float` which returns `None` on junk, and `None` never
  triggers a block.
- **Unknown cash buying power** → does not block on its own.
- **Dry-run** → short-circuits *before* preflight. No network, no account
  state. Keeps offline smoke tests offline.

Order results now carry a `pre_trade` key with the parsed estimates
(order value, estimated cost, buying power requirement, commission,
margin figures) regardless of whether the order was placed or blocked —
so callers can log them either way.

### Tests

`league_core/_equities_smoke.py` extended. Still fully offline. New cases:

- clean cash BUY allowed; details carry cost + buying-power requirement
- `marginUsageImpact > 0` blocks
- `initialMarginRequirement > 0` blocks
- `longInitialRequirement > 0` blocks
- same margin signal does **not** block when `cash_only=False`
- requirement > cash BP blocks; requirement == cash BP allowed (boundary)
- unknown cash BP does not block
- `NOT_SHORTABLE` blocks a SELL; ordinary SELL allowed
- empty response body does not block
- unparseable numerics do not block
- `_env_flag` parsing (unset/default, `false`, `yes`)
- dry-run unaffected by preflight env flags

Run:

```powershell
cd "C:\Users\Jeremiah\source\Trading Bot League"
python -m league_core._risk_smoke
python -m league_core._equities_smoke
```

Both should end `All cases passed.`

---

## Why stock/crypto stopped

Grepping the call sites for `place_market_sell_quantity` in
`bots/stock_momentum_v1/bot.py` found it is the **exit path for risk
management**:

| Line (approx) | Caller |
|---|---|
| 1462 | exit-all / STOP file |
| 1590 | momentum exit |
| **1662** | **DAILY_LOSS_LIMIT kill switch** |
| **1691** | **MAX_DRAWDOWN kill switch** |
| 1779 | dynamic take-profit |
| **1830** | **dynamic stop-loss** |

A preflight gate that can return "blocked" in front of that path could
prevent a stop-loss or a daily-loss liquidation from executing on a
transient API hiccup. That is the inverse of safety.

**The constraint:** preflight gates **BUYs**; for **SELLs** it logs
estimates and places the order regardless. Same principle already baked
into `league_core/risk.py`, where CLOSE actions (SELL/COVER) bypass the
daily-trade cap so emergency exits can never be blocked.

Secondary note: every call site wraps in `try/except` and logs
`ORDER_ERROR`. So raising is a viable signal for a declined BUY, but is
semantically wrong — a deliberate decline is not an error. Prefer a
structured "skipped" return.

For crypto, an open question to resolve first: **does Public's preflight
even support CRYPTO instruments?** The documented endpoint is
single-leg equity/options. Crypto is also likely cash-only at Public
already (no crypto margin), which may make the whole cash-only
enforcement a no-op there. Verify before writing code.

Both are queued as tasks with these constraints written into the task
descriptions.

---

## Files changed

- `league_core/public_api/equities.py` — preflight, cash-only buying
  power, pure rules engine, wiring into both order functions, expanded
  module docstring incl. the `useMargin` finding, expanded `__all__`
- `league_core/_equities_smoke.py` — ~20 new offline cases + updated
  docstring

Nothing else touched. No changes to stock, crypto, or any bot logic.

## Deploy

```powershell
cd "C:\Users\Jeremiah\source\Trading Bot League"
python -m league_core._equities_smoke     # expect "All cases passed."
git add -A
git commit -m "equities: preflight + cash-only pre-trade safety (documented approach, not useMargin)"
git push
fly deploy -a trading-bot-league-agent-runner
```

No new secrets required — all three env flags have safe defaults. Set
them on Fly only if you want to deviate:

```powershell
# e.g. to make preflight failures blocking:
fly secrets set EQUITIES_PREFLIGHT_STRICT=true -a trading-bot-league-agent-runner
```

## Effect on the running system

`etf_rotation_v1` is the only bot using this client. It is live but
**dormant** — SPY has not crossed its 50-day SMA since early June, so no
rebalance has triggered and there are zero live ETF trades to date. The
new checks therefore will not fire until the next regime change. That is
a good thing: the code ships and sits ready rather than being trialled
under pressure.

When the next regime change does fire, expect log lines like:

```
[public_equities] BUY SPY blocked by pre-trade check: preflight_margin_required {...}
```

if anything trips, and normal order placement otherwise.

## Still open

- Stock bot BUY-only preflight gate (task #3, constraint documented)
- Crypto bot — confirm preflight supports CRYPTO first (task #4)
- Wire `pre_trade` details into `bot_trades.metadata` so estimates land
  in Supabase alongside each trade
- Consider surfacing `shortSelling.availability` when/if
  `short_watchlist_v1` is ever promoted — the field is already parsed
  and a `PRE_NOT_SHORTABLE` code already exists
