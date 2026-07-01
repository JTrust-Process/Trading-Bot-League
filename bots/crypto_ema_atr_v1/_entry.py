# main.py
# Entry point. GitHub Actions runs this once per trigger.
# try/finally guarantees monitor.end_run() always fires, even on crash.
#
# Audit fix C1: previously end_run() always wrote status="completed",
# which silently overwrote any "error" status set by log_error(). We now
# track outcome explicitly and pass it through.

from dotenv import load_dotenv
load_dotenv()  # Must be first — all os.getenv() calls happen after this

from crypto_bot.core.engine import run
from crypto_bot.logging.monitor import Monitor
from crypto_bot.league import league_status  # ADDITIVE — fail-silent League heartbeat. Touches NO trading logic.

if __name__ == "__main__":
    monitor = Monitor()
    run_id = monitor.start_run()
    # League heartbeat — additive, fail-silent. Does NOT affect monitor.start_run()
    # or any trading logic. If the League project is unreachable, this no-ops.
    try:
        league_status.start_run("cron")
    except Exception:
        pass
    final_status = "completed"
    try:
        run(monitor, run_id)
        # Per-symbol exceptions are caught inside the engine and logged via
        # monitor.log_error(); they don't reach this except clause. Honor
        # those by reading the monitor's error flag.
        if monitor.had_error:
            final_status = "error"
    except Exception as e:
        final_status = "error"
        monitor.log_error(run_id, "engine", str(e))
        raise
    finally:
        monitor.end_run(run_id, status=final_status)
        # League end_run — additive, fail-silent. Maps the crypto bot's
        # "completed"/"error" vocabulary to League's "success"/"failed".
        try:
            league_status.end_run(
                status=("success" if final_status == "completed" else "failed"),
            )
        except Exception:
            pass