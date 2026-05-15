# agent_research_v1

**AI research bot.** Reads the last 24h of league state, asks Claude
to summarize and propose ideas, writes the brief and any proposals
into Supabase. **Never trades.** Never imports an order client.

## Triple-defense against execution

The "agent bots cannot trade" property is enforced in three independent
places. Any one is sufficient.

1. **Python** — `BotConfig.__post_init__` in `league_core/contracts.py`
   raises if a config is constructed with `bot_type='agent_research'` and
   `can_place_orders=True`.
2. **Database** — `bot_registry` has a CHECK constraint:
   `bot_type <> 'agent_research' OR can_place_orders = false`.
   See `supabase/migrations/001_bot_registry.sql`.
3. **Code** — this package imports `league_core.status` only for read
   and event/signal/approval writes. It does not import any order
   client, paper or live. There is no method available to call to
   place a trade.

If you ever see a `bot_trades` row attributed to `agent_research_v1`,
something has gone deeply wrong and you should kill the bot in the
registry immediately:

```sql
update bot_registry set status = 'killed' where bot_id = 'agent_research_v1';
```

## What it does

Each cycle:

1. Pulls the last 24h of `bot_research_scores`, `bot_signals`,
   `bot_events`, plus all open `bot_positions`, and the full
   `bot_registry` from the League Supabase project.
2. Compacts the snapshot to JSON (~2-5KB typical).
3. Sends it to Claude with a system prompt that constrains output to a
   structured JSON schema (brief + observations + 0-3 proposals).
4. Parses the response. Rejects proposals whose action is outside
   the allow-list, whose strategy name contains denied substrings
   ('naked', 'margin', 'leveraged', 'concentrated'), or that are
   structurally malformed.
5. Writes the brief as a single `AGENT_BRIEF` event in `bot_events`.
6. Writes each surviving proposal as TWO rows: an `agent_proposal`
   signal in `bot_signals` (audit trail) AND a `pending` row in
   `bot_approvals` (what surfaces in the dashboard for human review).

## Allowed proposal actions

| Action          | Meaning                                              |
|-----------------|------------------------------------------------------|
| `PAPER_BUY`     | Long entry in a paper bot's universe                 |
| `PAPER_SELL`    | Long exit                                            |
| `PAPER_SHORT`   | Short entry (paper only — short_watchlist_v1)        |
| `PAPER_COVER`   | Short cover                                          |
| `OPTION_OPEN`   | Defined-risk options trade idea                      |
| `OPTION_CLOSE`  | Close an existing options position                   |
| `WATCH`         | "No action, but pay attention to this"               |

Anything else is rejected at parse time, regardless of what the LLM
returned.

## Approval flow

1. Bot writes a row to `bot_approvals` with `status='pending'`.
2. Dashboard's **Pending approvals** section shows the row with a
   payload preview + Approve / Reject buttons.
3. You enter your operator token, decide, and click. The Next.js Route
   Handler at `/api/approvals/[id]` flips the row to `approved` or
   `rejected` server-side using the service-role key.
4. **There is no execution bot consuming approvals yet.** Approving a
   proposal is a "I would have done this" audit record. A future
   `agent_execution_v1` bot would read `status='approved'` rows from
   `bot_approvals` and route them to the appropriate paper bot's
   simulated fill engine — but that bot doesn't exist and is not on
   the near roadmap.

So for now: this is a daily AI research note + a queue of ideas you can
manually click through. The structure is in place for execution later.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `LEAGUE_SUPABASE_URL`  |       | League Supabase URL. |
| `LEAGUE_SUPABASE_KEY`  |       | League service-role key. |
| `LEAGUE_BOT_ID`        |       | Must equal `agent_research_v1`. |
| `ANTHROPIC_API_KEY`    |       | Anthropic API key (`sk-ant-api03-...`). |
| `AGENT_MODEL`          | `claude-haiku-4-5-20251001` | Override to use Sonnet / Opus. |
| `AGENT_MAX_PROPOSALS`  | `3`   | Hard cap regardless of LLM output. |

Cost estimate: Haiku, ~2-5KB context + ~1-2KB output per run = roughly
$0.003 per run. Daily = ~$1/year. If you switch to Sonnet, expect
roughly $0.02/run = ~$5-10/year.

## Schedule

Once per day, `cron: "50 14 * * 1-5"` (10:50 ET during EDT). Runs
*after* the other research bots (bond_research at :35, options_alert
at :43) so the agent sees their latest output in its context.

## Limitations

- No tool use. The LLM has no ability to fetch additional data
  mid-conversation; it must work from the single snapshot we provide.
  This is intentional — it keeps the bot's behavior deterministic and
  its output strictly grounded in the same data a human would see on
  the dashboard.
- One model call per cycle. No multi-turn refinement.
- No memory across cycles. The agent doesn't know what it proposed
  yesterday; each run is a fresh look at the league state. A future
  enhancement would include "your last N briefs" in the context.
- JSON parsing is best-effort. If the LLM returns malformed output,
  the bot writes an `AGENT_PARSE_FAILED` event with the raw head and
  exits with status='warning'. No crash, just no brief that day.
