"""scripts/ — standalone Python entry points (league_health, leaderboard_snapshot, etc).

This file is intentionally tiny — it exists so the agent_runner service
can do `from scripts.league_health import main` when running the health
check on its internal schedule. Without it, `scripts/` is just a folder
on disk and not a Python package.
"""
