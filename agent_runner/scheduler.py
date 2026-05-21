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

@contextmanager
def _league_bot_id(bot_id: str):
    """Temporarily set LEAGUE_BOT_ID for the duration of a single job run.

    Restores the previous value on exit. NOT thread-safe — relies on
    APScheduler being configured with max_workers=1."""
    prev = os.environ.get("LEAGUE_BOT_ID")
    os.environ["LEAGUE_BOT_ID"] = bot_id
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("LEAGUE_BOT_ID", None)
        else:
            os.environ["LEAGUE_BOT_ID"] = prev


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
        with _league_bot_id(bot_id):
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
    def _shutdown(signum, _frame):
        log.info("received signal %s, shutting down scheduler", signum)
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
