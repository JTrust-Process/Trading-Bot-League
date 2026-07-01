# crypto_bot/logging/monitor.py
#
# Tracks bot run lifecycle in Supabase.
#
# Fixes from audit:
#   - Issue 15: uses shared _supabase helper instead of duplicating client logic
#   - Audit C1: had_error flag lets main.py pass correct status to end_run().
#     Previously, log_error() set bot_runs.status="error" but end_run() in the
#     finally block overwrote it back to "completed", masking failed runs.

from crypto_bot.logging._supabase import get_client, safe_insert, now_iso


class Monitor:
    def __init__(self) -> None:
        # Audit C1: set whenever log_error fires so main.py can pass the right
        # status into end_run(). Per-symbol exceptions don't propagate to main,
        # so without this flag those errors stayed hidden in bot_errors and the
        # run row showed "completed".
        self.had_error: bool = False

    # ── Run lifecycle ─────────────────────────────────────────────────────────

    def start_run(self) -> str | None:
        """
        Insert a bot_runs row with status='running'.
        Returns the new row's id, or None on failure.
        """
        try:
            resp = (
                get_client()
                .table("bot_runs")
                .insert({"started_at": now_iso(), "status": "running"})
                .execute()
            )
            rows = resp.data
            run_id: str | None = None
            if rows and isinstance(rows, list) and isinstance(rows[0], dict):
                raw_id = rows[0].get("id")
                if raw_id is not None:
                    run_id = str(raw_id)
            print(f"[monitor] Run started — id={run_id}")
            return run_id
        except Exception as e:
            print(f"[monitor] start_run failed: {e}")
            return None

    def end_run(self, run_id: str | None, status: str = "completed") -> None:
        if run_id is None:
            return
        try:
            get_client().table("bot_runs").update(
                {"ended_at": now_iso(), "status": status}
            ).eq("id", run_id).execute()
            print(f"[monitor] Run ended — id={run_id} status={status}")
        except Exception as e:
            print(f"[monitor] end_run failed: {e}")

    # ── Events & errors ───────────────────────────────────────────────────────

    def log_event(self, run_id: str | None, event_type: str, message: str) -> None:
        safe_insert("bot_events", {
            "run_id":     run_id,
            "event_type": event_type,
            "message":    message,
            "created_at": now_iso(),
        })

    def log_error(self, run_id: str | None, context: str, error: str) -> None:
        # Mark this run as failed so end_run() doesn't overwrite the status
        # back to "completed" in the finally block. (Audit C1)
        self.had_error = True

        safe_insert("bot_errors", {
            "run_id":     run_id,
            "context":    context,
            "error":      error,
            "created_at": now_iso(),
        })
        # Escalate run status so dashboard can surface it immediately, even
        # before end_run() runs at the very end of the cycle.
        if run_id:
            try:
                get_client().table("bot_runs").update(
                    {"status": "error"}
                ).eq("id", run_id).execute()
            except Exception:
                pass