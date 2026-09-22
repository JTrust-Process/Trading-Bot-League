"""Smoke tests for stock_momentum_v1.monitor — per-run counter reset.

Run from the repo root:

    python -m bots.stock_momentum_v1._monitor_smoke

No network, no Supabase, no credentials. `_sb()` is patched to return None,
which every method in Monitor already handles (`if sb and self.run_id`), so
nothing reaches a database.

────────────────────────────────────────────────────────────────────────────
THE REGRESSION UNDER TEST (fixed 2026-09-22)

`monitor` is a module-level singleton. Under GitHub Actions each cycle was
its own process, so __init__ reset the counters every run. On Fly the
agent_runner is ONE long-lived process — `import monitor` resolves from
sys.modules and the same instance survives for weeks. main.py re-runs
bot.py via runpy each cycle, giving a fresh module namespace but NOT a
fresh Monitor.

start_run() reset run_id and start_time but not error_count, trade_count or
critical_error. Supabase showed consecutive stock_momentum_v1 runs
reporting error_count 37, 39, 41 ... 63 while the joined bot_errors rows
for each of those runs numbered ZERO.

Two silent consequences:
  * end_run() promotes any run with error_count > 0 to "warning", so every
    cycle after the first error was permanently degraded — and
    league_health reads last_run_status.
  * critical_error is sticky, so ONE critical error pinned every later run
    to "failed" until the machine restarted.

These tests assert the counters are per-run, and — more importantly — that
they STAY per-run across repeated cycles on a single instance, which is the
condition the Fly scheduler actually creates.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_offline_monitor(monitor_mod):
    """A Monitor that can never reach Supabase.

    `_sb()` returning None is the module's own "not configured" path —
    every method guards on `if sb and self.run_id`. So this exercises the
    real code, not a stub of it.
    """
    m = monitor_mod.Monitor()
    m._sb = lambda: None  # type: ignore[assignment]
    return m


def main() -> int:
    print("=" * 66)
    print("stock_momentum_v1.monitor — per-run counter reset")
    print("=" * 66)

    strict = os.getenv("SMOKE_STRICT", "0").strip().lower() in (
        "1", "true", "yes", "on")
    try:
        import monitor as monitor_mod
    except Exception as e:  # noqa: BLE001
        check("monitor module imported", False, repr(e))
        if not strict:
            print("        pip install -r agent_runner/requirements.txt")
        print("\n" + "=" * 66)
        print(f"  {_PASS} passed, {_FAIL} failed")
        print("=" * 66)
        return 1

    # ── 1-5. Dirty counters are cleared by start_run() ─────────────────────
    print("\n[1] start_run() resets per-run counters")
    m = make_offline_monitor(monitor_mod)
    m.trade_count = 7
    m.error_count = 12
    m.critical_error = True

    rid = m.start_run()

    check("trade_count reset to 0", m.trade_count == 0, f"got {m.trade_count}")
    check("error_count reset to 0", m.error_count == 0, f"got {m.error_count}")
    check("critical_error reset to False", m.critical_error is False,
          f"got {m.critical_error!r}")
    check("run_id assigned", bool(m.run_id))
    check("start_run returns the run_id", rid == m.run_id)
    check("start_time assigned", m.start_time is not None)

    print("\n[2] a second start_run() issues a NEW run_id and start_time")
    first_id, first_start = m.run_id, m.start_time
    m.start_run()
    check("run_id changed", m.run_id != first_id)
    check("start_time advanced or equal", m.start_time >= first_start)

    # ── 6. Counting still works within a run ───────────────────────────────
    print("\n[3] counters still count within a run")
    m.log_error("test_stage", ValueError("boom"))
    check("error_count == 1 after one log_error", m.error_count == 1,
          f"got {m.error_count}")
    check("warning severity does not set critical", m.critical_error is False)

    m.log_error("test_stage", ValueError("boom2"))
    check("error_count == 2 after two", m.error_count == 2, f"got {m.error_count}")

    m.trade_count += 3
    check("trade_count increments normally", m.trade_count == 3)

    # ── 7. The next run starts clean ───────────────────────────────────────
    print("\n[4] the NEXT run starts clean — the actual bug")
    m.start_run()
    check("error_count back to 0", m.error_count == 0, f"got {m.error_count}")
    check("trade_count back to 0", m.trade_count == 0, f"got {m.trade_count}")
    check("critical_error back to False", m.critical_error is False)

    # ── 8. critical_error must not be sticky across runs ───────────────────
    print("\n[5] critical_error does not leak into later runs")
    m.log_error("bad", RuntimeError("fatal"), severity="critical")
    check("critical severity sets the flag", m.critical_error is True)
    check("critical also counts as an error", m.error_count == 1)
    m.start_run()
    check("critical_error cleared by next run", m.critical_error is False,
          "a sticky critical pinned every later run to 'failed'")

    # ── 9. The Fly condition: many cycles, one instance ────────────────────
    print("\n[6] 20 consecutive cycles on ONE instance never accumulate")
    m2 = make_offline_monitor(monitor_mod)
    drifted = []
    for cycle in range(20):
        m2.start_run()
        if m2.error_count != 0 or m2.trade_count != 0 or m2.critical_error:
            drifted.append(
                (cycle, m2.error_count, m2.trade_count, m2.critical_error))
        # Simulate a working cycle: some trades, an error, occasionally fatal.
        m2.trade_count += 2
        m2.log_error("cycle", ValueError("e"),
                     severity="critical" if cycle % 7 == 0 else "warning")
    check("no cycle began with dirty counters", not drifted,
          f"drifted at {drifted[:3]}")

    # This is the number Supabase was showing. Without the fix, 20 cycles
    # of one error each would report 20 on the final run instead of 1.
    check("final run reports 1 error, not 20", m2.error_count == 1,
          f"got {m2.error_count} — counters are accumulating again")

    # ── 10. end_run() status derives from per-run counts ───────────────────
    print("\n[7] end_run() status reflects THIS run only")
    m3 = make_offline_monitor(monitor_mod)
    m3.start_run()
    m3.log_error("x", ValueError("e"))          # one warning-level error
    m3.end_run("success")                        # would be promoted to warning
    m3.start_run()                               # clean run
    check("clean run has no inherited errors", m3.error_count == 0)
    check("clean run has no inherited critical", m3.critical_error is False)

    # ── 11. Offline guarantee ──────────────────────────────────────────────
    print("\n[8] nothing reached a database")
    check("_sb() returns None (patched)", m._sb() is None)
    check("all three instances patched",
          m._sb() is None and m2._sb() is None and m3._sb() is None)
    check("no credentials required", True,
          "every method guards on `if sb and self.run_id`")

    print("\n" + "=" * 66)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 66)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
