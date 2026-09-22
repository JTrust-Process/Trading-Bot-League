import uuid
import time
from datetime import datetime, timezone
from typing import Optional, Any, Callable
from supabase import create_client
import os


def _get_supabase_client():
    """Lazy init — called only after load_dotenv() has run in main()."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[MONITOR] Supabase init failed: {e}")
        return None


class Monitor:
    def __init__(self):
        self.run_id = None
        self.start_time = None
        self.error_count = 0
        self.trade_count = 0
        self.critical_error = False
        self._client = None

    def _sb(self):
        """Return Supabase client, initializing lazily on first use."""
        if self._client is None:
            self._client = _get_supabase_client()
        return self._client

    def start_run(self):
        # ── PER-RUN COUNTER RESET ──────────────────────────────────────────
        #
        # FIXED 2026-09-22. These three lines were missing, and their absence
        # was invisible until someone compared reported counts against the
        # rows they were supposedly counting.
        #
        # `monitor` is a MODULE-LEVEL SINGLETON (bottom of this file). Under
        # GitHub Actions each cycle was its own process, so a fresh Monitor
        # was constructed every run and __init__ did the resetting. On Fly
        # the agent_runner is ONE long-lived process: `import monitor`
        # resolves from sys.modules, so the same instance survives for
        # weeks. main.py re-executes bot.py via runpy each cycle, which
        # creates a fresh module namespace but NOT a fresh Monitor.
        #
        # start_run() reset run_id and start_time but not the counters, so
        # error_count and trade_count accumulated monotonically from
        # process start. Supabase showed stock_momentum_v1 reporting
        # error_count 37, 39, 41 ... 63 across consecutive runs while the
        # joined bot_errors rows for each of those runs numbered ZERO.
        #
        # Two consequences, both silent:
        #   1. end_run() promotes any run with error_count > 0 to "warning",
        #      so every cycle after the first error was permanently degraded
        #      — and league_health reads last_run_status.
        #   2. critical_error is sticky, so ONE critical error pinned every
        #      subsequent run to "failed" until the machine restarted.
        #
        # This is the same class of post-migration regression as the
        # GITHUB_REF_NAME state-key fallback and the APScheduler day-of-week
        # off-by-one: code whose correctness depended on an assumption
        # ("each run is a fresh process") that the move to Fly quietly broke.
        # Nothing errored; a surface just reported something untrue.
        #
        # Reset BEFORE the bot_runs insert so the new row and the in-memory
        # counters agree from the first instant of the run.
        self.error_count = 0
        self.trade_count = 0
        self.critical_error = False

        self.run_id = str(uuid.uuid4())
        self.start_time = datetime.now(timezone.utc)
        try:
            sb = self._sb()
            if sb:
                sb.table("bot_runs").insert({
                    "id": self.run_id,
                    "start_time": self.start_time.isoformat(),
                    "status": "running",
                }).execute()
        except Exception as e:
            print(f"[MONITOR] start_run failed: {e}")
        self.log_event("BOT_START")
        return self.run_id

    def end_run(self, status: str = "success"):
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - self.start_time).total_seconds() * 1000) if self.start_time else 0
        if self.critical_error:
            status = "failed"
        elif self.error_count > 0 and status == "success":
            status = "warning"
        try:
            sb = self._sb()
            if sb and self.run_id:
                sb.table("bot_runs").update({
                    "end_time": end_time.isoformat(),
                    "status": status,
                    "total_trades": self.trade_count,
                    "total_errors": self.error_count,
                    "duration_ms": duration_ms,
                }).eq("id", self.run_id).execute()
        except Exception as e:
            print(f"[MONITOR] end_run failed: {e}")
        self.log_event("BOT_END", metadata={"status": status, "trades": self.trade_count, "errors": self.error_count})

    def log_event(self, event_type: str, symbol: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        try:
            sb = self._sb()
            if sb and self.run_id:
                sb.table("bot_events").insert({
                    "id": str(uuid.uuid4()),
                    "run_id": self.run_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event_type": event_type,
                    "symbol": symbol,
                    "metadata": metadata,
                }).execute()
        except Exception as e:
            print(f"[MONITOR] log_event failed: {e}")

    def log_error(self, stage: str, error: Exception, symbol: Optional[str] = None, severity: str = "warning", retry_count: int = 0) -> None:
        self.error_count += 1
        if severity == "critical":
            self.critical_error = True
        try:
            sb = self._sb()
            if sb and self.run_id:
                sb.table("bot_errors").insert({
                    "id": str(uuid.uuid4()),
                    "run_id": self.run_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "stage": stage,
                    "symbol": symbol,
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "severity": severity,
                    "retry_count": retry_count,
                }).execute()
        except Exception as e:
            print(f"[MONITOR] log_error failed: {e}")

    def safe_execute(self, stage: str, func: Callable[..., Any], *args: Any, symbol: Optional[str] = None, retries: int = 3, **kwargs: Any) -> Any:
        for attempt in range(retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                self.log_error(stage, e, symbol=symbol, retry_count=attempt)
                if attempt < retries - 1:
                    self.log_event("RETRY_TRIGGERED", symbol=symbol, metadata={"attempt": attempt + 1})
                    time.sleep(2 ** attempt)
                else:
                    self.log_error(stage, e, symbol=symbol, severity="critical", retry_count=attempt)
                    raise


monitor = Monitor()
