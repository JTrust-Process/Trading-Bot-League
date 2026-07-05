"""agent_runner/scheduler.py — APScheduler driver.

Wraps each bot's existing run_cycle() function in an APScheduler job on
its previous cron cadence. The bots are not modified — we just import
their entry function and call it.

Concurrency: we set max_workers=1 because the bots read LEAGUE_BOT_ID
from process-wide os.environ (see league_core/status.py._config). Running
two bot jobs in parallel would race on that env var. Our cron load is
tiny — at most 6 jobs scheduled per hour — so serial execution is fine.

Logging: stdout only. Fly.io captures stdout into its log dashboard.
APScheduler's own logger is wired in so missed-fire and job-error events
are visible.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Make repo root importable. agent_runner/scheduler.py is one level deep,
# so the parent dir is the repo root where bots/, league_core/, scripts/
# all live.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_EXECUTED


# ── Logging setup ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
# UTC timestamps in log lines — match what we write to bot_runs/bot_status.
logging.Formatter.converter = time.gmtime

log = logging.getLogger("agent_runner")
logging.getLogger("apscheduler").setLevel(logging.INFO)


# ── Bot imports — lazy so a single broken bot doesn't crash the whole runner ─

def _safe_import(module_path: str, attr: str):
    """Import module_path and return getattr(module, attr), or None on failure."""
    try:
        mod = __import__(module_path, fromlist=[attr])
        return getattr(mod, attr)
    except Exception as e:  # noqa: BLE001
        log.error("Could not import %s.%s: %s", module_path, attr, e)
        return None


# We delay binding the run_cycle callables until job invocation so that
# a broken import surfaces as a missed run (not a crashed scheduler).


# ── Env-var swap helper ────────────────────────────────────────────────────
#
# Two concerns handled by _bot_env_scope below:
#
# 1. LEAGUE_BOT_ID — every bot's League adapter reads this to know which
#    bot_registry / bot_status row to write to. Swapped per job so a
#    single scheduler process can host bots with different bot_ids.
#
# 2. Per-bot env prefix stripping — some bots read env vars whose names
#    COLLIDE across bots on a single Fly process:
#      - stock_momentum_v1 and crypto_ema_atr_v1 both read SUPABASE_URL
#        (different Supabase projects), SUPABASE_KEY/SUPABASE_SERVICE_KEY
#        (different keys), SYMBOLS ("QQQ,SPY,..." vs "BTC"),
#        MAX_ORDER_AMOUNT_USD ($15 vs $25), ATR_PERIOD (semantic diff),
#        and other tuning vars.
#      - On GHA each bot had its own workflow env; on Fly they share one
#        process.
#
#    Solution: set every bot-specific var on Fly (as a secret OR in
#    fly.toml [env]) with the bot's prefix — `STOCK_` for
#    stock_momentum_v1, `CRYPTO_` for crypto_ema_atr_v1. The scheduler
#    auto-strips the prefix for the duration of that bot's job.
#    `STOCK_MAX_ORDER_AMOUNT_USD` becomes `MAX_ORDER_AMOUNT_USD` while
#    the stock bot runs, then reverts. Vendored bot code stays
#    byte-identical — no renames inside the source.
#
#    Adding a new tuning var? Just set `STOCK_NEWTHING=...` (or
#    `CRYPTO_NEWTHING=...`) on Fly. No scheduler.py edit needed.
#
#    Adding a new bot that needs this pattern? Add its prefix to
#    _BOT_ENV_PREFIX below.
#
# Shared env vars (PUBLIC_SECRET, PUBLIC_ACCOUNT_ID, DISCORD_WEBHOOK_URL,
# LEAGUE_SUPABASE_URL/KEY, TZ, etc.) are NOT prefixed — every bot reads
# them under the same name, so we set them once as flat Fly secrets or
# fly.toml [env] entries.

_BOT_ENV_PREFIX: dict[str, str] = {
    "stock_momentum_v1":  "STOCK_",
    "crypto_ema_atr_v1":  "CRYPTO_",
}


@contextmanager
def _bot_env_scope(bot_id: str):
    """Temporarily set LEAGUE_BOT_ID plus any prefix-stripped env
    overrides for the duration of one job run. See _BOT_ENV_PREFIX and
    the module-level notes for the design.

    For a bot with prefix `STOCK_`, EVERY env var starting with
    `STOCK_` is copied to its unprefixed name for the job's lifetime
    (`STOCK_SYMBOLS` → `SYMBOLS`, `STOCK_SUPABASE_URL` → `SUPABASE_URL`,
    etc.). Overrides win over any pre-existing unprefixed value; prior
    values are restored on exit.

    NOT thread-safe — relies on APScheduler being configured with
    max_workers=1 (see build_scheduler below).
    """
    # Start with the mandatory LEAGUE_BOT_ID override.
    overrides: dict[str, str] = {"LEAGUE_BOT_ID": bot_id}

    # Auto-strip the bot's env prefix from every matching env var.
    prefix = _BOT_ENV_PREFIX.get(bot_id)
    if prefix:
        for src_key, val in list(os.environ.items()):
            if src_key.startswith(prefix):
                target_key = src_key[len(prefix):]
                # Skip an env var that's literally just the prefix
                # (unlikely, but a value like "STOCK_" would map to "").
                if target_key:
                    overrides[target_key] = val

    prev = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, prev_v in prev.items():
            if prev_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev_v


# ── Job wrappers ───────────────────────────────────────────────────────────

def _run_bot(bot_id: str, module_path: str, attr: str = "run_cycle") -> None:
    """Generic wrapper: set LEAGUE_BOT_ID, call run_cycle(), log result.

    Exceptions are caught and logged so a single crashing bot doesn't
    take down the scheduler. APScheduler also catches exceptions from
    jobs via its EVENT_JOB_ERROR listener, but we want a tight inner
    try/except so the bot's own monitor.end_run() gets a chance to mark
    its bot_runs row as 'failed' before we lose the stack."""
    log.info("→ job start: %s", bot_id)
    fn = _safe_import(module_path, attr)
    if fn is None:
        log.error("× job aborted: could not import %s.%s", module_path, attr)
        return
    try:
        with _bot_env_scope(bot_id):
            result = fn()
        log.info("✓ job done: %s status=%s", bot_id, result)
    except Exception as e:  # noqa: BLE001
        log.error("× job crashed: %s err=%r", bot_id, e)
        traceback.print_exc()


def _run_league_health() -> None:
    """league_health.main() is in scripts/, not bots/. It returns an int.

    Unlike the bots, league_health doesn't write to bot_runs/bot_status for
    itself — it's an observer of other bots. So we don't wrap it with
    _league_bot_id."""
    log.info("→ job start: league_health")
    fn = _safe_import("scripts.league_health", "main")
    if fn is None:
        log.error("× league_health aborted: import failed")
        return
    try:
        rc = fn()
        log.info("✓ league_health done rc=%s", rc)
    except Exception as e:  # noqa: BLE001
        log.error("× league_health crashed err=%r", e)
        traceback.print_exc()


# ── APScheduler event hooks (for visibility) ───────────────────────────────

def _on_job_event(event):
    if event.code == EVENT_JOB_ERROR:
        log.error("APScheduler caught job error in %s: %s", event.job_id, event.exception)
    elif event.code == EVENT_JOB_MISSED:
        log.warning("APScheduler missed run for %s scheduled at %s",
                    event.job_id, event.scheduled_run_time)
    elif event.code == EVENT_JOB_EXECUTED:
        # Already logged inside the wrapper; keep this quiet.
        pass


# ── Main ───────────────────────────────────────────────────────────────────

def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,           # if multiple fires are pending, run once
            "max_instances": 1,         # one running copy of a given job at a time
            "misfire_grace_time": 600,  # 10 min — generous, but skip if older
        },
        executors={
            # max_workers=1 → only one job runs at any moment across the
            # entire scheduler. Required because the bots share LEAGUE_BOT_ID
            # via os.environ.
            "default": {"type": "threadpool", "max_workers": 1},
        },
    )

    # ── Daily research bots — weekday only, single fire per day ─────────────
    sched.add_job(
        _run_bot, args=("bond_research_v1", "bots.bond_research_v1.main"),
        trigger=CronTrigger.from_crontab("35 14 * * 1-5", timezone="UTC"),
        id="bond_research_v1", name="bond_research_v1 (daily)",
    )
    sched.add_job(
        _run_bot, args=("options_alert_v1", "bots.options_alert_v1.main"),
        trigger=CronTrigger.from_crontab("43 14 * * 1-5", timezone="UTC"),
        id="options_alert_v1", name="options_alert_v1 (daily)",
    )
    sched.add_job(
        _run_bot, args=("agent_research_v1", "bots.agent_research_v1.main"),
        trigger=CronTrigger.from_crontab("50 14 * * 1-5", timezone="UTC"),
        id="agent_research_v1", name="agent_research_v1 (daily)",
    )

    # ── Hourly paper bots — weekday market hours ────────────────────────────
    sched.add_job(
        _run_bot, args=("etf_rotation_v1", "bots.etf_rotation_v1.main"),
        trigger=CronTrigger.from_crontab("33 14-20 * * 1-5", timezone="UTC"),
        id="etf_rotation_v1", name="etf_rotation_v1 (hourly mkt hrs)",
    )
    sched.add_job(
        _run_bot, args=("short_watchlist_v1", "bots.short_watchlist_v1.main"),
        trigger=CronTrigger.from_crontab("41 14-20 * * 1-5", timezone="UTC"),
        id="short_watchlist_v1", name="short_watchlist_v1 (hourly mkt hrs)",
    )

    # ── Shadow logger — DISABLED 2026-05-28 ─────────────────────────────────
    # public_shadow_v1 polled Public account #2 to mirror AI-tool trades into
    # bot_trades. Account #2 was reassigned to an off-platform strategy and
    # the Claude MCP path was dropped, so there's nothing for the logger to
    # capture. The bot code remains in `bots/public_shadow_v1/` for future
    # revival; if you re-enable, restore the add_job block from git history
    # and flip `bot_registry.status` back to 'enabled'.

    # ── Live bots migrated from GHA — GATED by LIVE_BOTS_ENABLED env ────────
    # stock_momentum_v1 and crypto_ema_atr_v1 were vendored into this repo on
    # 2026-06-01 from `Trading Bot/Trading Bot Project/` and
    # `Crypto_Trading_Project/Crypto_Trading_Bot/`. They are NOT scheduled
    # here by default — only when `LIVE_BOTS_ENABLED` is a truthy env var.
    #
    # DANGER: enabling this while the corresponding GHA workflows are STILL
    # running the same crons means BOTH schedulers will fire the live cycle.
    # The stock bot's deterministic uuid5 order_id dedupes same-minute
    # duplicates on Public's side, but any timing drift into a different
    # minute would allow both to fill. See agent_runner/README.md § Cutover
    # for the safe sequence: disable GHA crons FIRST, then flip this flag.
    if os.getenv("LIVE_BOTS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        sched.add_job(
            _run_bot, args=("stock_momentum_v1", "bots.stock_momentum_v1.main"),
            trigger=CronTrigger.from_crontab("17 14-20 * * 1-5", timezone="UTC"),
            id="stock_momentum_v1",
            name="stock_momentum_v1 (weekday :17 mkt hrs)",
        )
        sched.add_job(
            _run_bot, args=("crypto_ema_atr_v1", "bots.crypto_ema_atr_v1.main"),
            trigger=CronTrigger.from_crontab("7,22,37,52 * * * *", timezone="UTC"),
            id="crypto_ema_atr_v1",
            name="crypto_ema_atr_v1 (every 15 min 24/7)",
        )
        log.info("LIVE_BOTS_ENABLED — stock_momentum_v1 and crypto_ema_atr_v1 SCHEDULED")
    else:
        log.info(
            "LIVE_BOTS_ENABLED not set — stock_momentum_v1 and crypto_ema_atr_v1 "
            "vendored but NOT scheduled on Fly (still running on GHA)"
        )

    # ── League health — every 15 min, 24/7 ──────────────────────────────────
    sched.add_job(
        _run_league_health,
        trigger=CronTrigger.from_crontab("9,24,39,54 * * * *", timezone="UTC"),
        id="league_health", name="league_health (every 15 min)",
    )

    sched.add_listener(_on_job_event,
                       EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_EXECUTED)
    return sched


def _print_startup_banner(sched: BlockingScheduler) -> None:
    log.info("=" * 60)
    log.info("agent_runner starting up")
    log.info("UTC now: %s", datetime.now(timezone.utc).isoformat())
    log.info("python: %s", sys.version.split()[0])
    log.info("repo root: %s", _ROOT)
    log.info("jobs scheduled:")
    now = datetime.now(timezone.utc)
    for j in sched.get_jobs():
        # next_run_time is only computed after sched.start() — pre-start
        # jobs are "tentative" and don't have it set. Compute it from the
        # trigger directly so the banner is informative either way.
        try:
            next_fire = j.trigger.get_next_fire_time(None, now)
        except Exception:
            next_fire = None
        log.info("  %-26s next=%s  trigger=%s", j.id, next_fire, j.trigger)
    log.info("=" * 60)


def main() -> int:
    sched = build_scheduler()
    _print_startup_banner(sched)

    # Clean shutdown on SIGTERM (Fly.io) or SIGINT (local Ctrl+C).
    # sys.exit(0) here is RIGHT — Fly sent us a signal because it wants
    # us to stop, not because we crashed. Non-zero on signal would loop us.
    def _shutdown(signum, _frame):
        log.info("received signal %s, shutting down scheduler", signum)
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # sched.start() is meant to block forever. If it ever returns on its
    # own — meaning APScheduler exited without a signal — that's a bug we
    # want Fly to learn about by RESTARTING the machine, not by quietly
    # exiting 0 and (depending on machine restart policy) leaving the
    # service down. So we signal failure on any unexpected return.
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        # SystemExit re-raised by _shutdown — already handled above.
        raise
    except BaseException as e:  # noqa: BLE001
        log.exception("scheduler.start() raised: %r — exiting non-zero so Fly restarts", e)
        return 2
    # sched.start() returned without raising — that should never happen.
    log.error("scheduler.start() returned without raising — exiting non-zero so Fly restarts")
    return 3


if __name__ == "__main__":
    sys.exit(main())
