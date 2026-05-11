"""scripts/league_health.py

GHA cron job that reads the Trading Bot League's bot_registry and bot_status
tables and pings Discord on health transitions (healthy ↔ degraded ↔ down).

Cadence: every 15 minutes.

State: status_state.json persisted via GHA cache. Used to de-duplicate
pings so a stuck bot doesn't spam every cycle — we ping on transitions
and at most every COOLDOWN_HOURS while still in a bad state.

The script is read-only against the League Supabase project. It does not
restart bots, modify registry rows, or write any data back. The auto-
restart pattern in the existing health_check.py is deliberately not
mirrored here (per the safety policy: financial bots being auto-restarted
during an incident can cause wrong-state issues).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests


# ── Config ────────────────────────────────────────────────────────────────────

STATE_FILE = "status_state.json"

# Heartbeat-age thresholds. Mirror the dashboard so users see the same
# verdict on the page and in their Discord pings.
HEARTBEAT_DEGRADED_MIN = 30
HEARTBEAT_DOWN_MIN     = 120

# Max ping frequency while a bot stays in the same bad state.
COOLDOWN_HOURS = 6

# Failed-run / error escalations
RUN_STATUS_DOWN = {"failed", "timeout"}
RUN_STATUS_WARN = {"warning"}

# HTTP timeouts
TIMEOUT_SUPABASE = 8.0
TIMEOUT_DISCORD  = 5.0


# ── Models ────────────────────────────────────────────────────────────────────


@dataclass
class BotView:
    bot_id: str
    bot_name: str
    bot_type: str
    mode: str
    registry_status: str          # 'enabled' | 'disabled' | 'killed'
    status_row: Optional[dict]    # bot_status row, or None

    @property
    def display_name(self) -> str:
        return self.bot_name or self.bot_id


# ── Supabase reads ────────────────────────────────────────────────────────────


def _config() -> Optional[dict[str, str]]:
    """Read env vars at call time. Mirrors the adapter's lazy pattern."""
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    if not url or not key:
        return None
    return {"url": url, "key": key}


def _get(cfg: dict[str, str], path: str) -> Optional[list[dict]]:
    """GET via PostgREST. Returns the parsed JSON list or None on error."""
    url = f"{cfg['url']}/rest/v1/{path}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_SUPABASE)
    except Exception as e:  # noqa: BLE001
        print(f"[league_health] GET {path} failed: {e!r}")
        return None
    if resp.status_code >= 400:
        print(f"[league_health] GET {path} status={resp.status_code} body={resp.text[:200]}")
        return None
    try:
        return resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[league_health] GET {path} json decode failed: {e!r}")
        return None


def fetch_views(cfg: dict[str, str]) -> list[BotView]:
    """Join bot_registry with bot_status into a list of BotView objects."""
    registry = _get(cfg, "bot_registry?select=*") or []
    statuses = _get(cfg, "bot_status?select=*") or []
    status_by_id = {s["bot_id"]: s for s in statuses if isinstance(s, dict)}
    out: list[BotView] = []
    for r in registry:
        if not isinstance(r, dict):
            continue
        out.append(BotView(
            bot_id=r.get("bot_id") or "",
            bot_name=r.get("bot_name") or "",
            bot_type=r.get("bot_type") or "",
            mode=r.get("mode") or "",
            registry_status=r.get("status") or "enabled",
            status_row=status_by_id.get(r.get("bot_id")),
        ))
    return out


# ── Health derivation ─────────────────────────────────────────────────────────


@dataclass
class Verdict:
    bucket: str       # 'healthy' | 'degraded' | 'down' | 'idle'
    label: str        # short human label
    reasons: list[str]


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        # PostgREST returns ISO with TZ; datetime.fromisoformat handles "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def derive(view: BotView) -> Verdict:
    reasons: list[str] = []

    if view.registry_status == "killed":
        return Verdict("down", "Killed", ["Operator killed this bot."])
    if view.registry_status == "disabled":
        return Verdict("idle", "Disabled", ["Bot is disabled in registry."])

    s = view.status_row
    if not s:
        return Verdict("idle", "No heartbeat", ["Bot has not heartbeat yet."])

    last_hb_ts = _parse_ts(s.get("last_heartbeat_at"))
    age_min = ((time.time() - last_hb_ts) / 60.0) if last_hb_ts else None
    last_status = s.get("last_run_status") or ""
    last_err = (s.get("last_error_msg") or "")[:200]

    if age_min is None:
        return Verdict("idle", "No heartbeat", ["Last heartbeat unknown."])

    if age_min > HEARTBEAT_DOWN_MIN:
        return Verdict("down", "Stale", [f"Last heartbeat {int(age_min)}m ago."])

    if age_min > HEARTBEAT_DEGRADED_MIN:
        reasons.append(f"Last heartbeat {int(age_min)}m ago.")

    if last_status in RUN_STATUS_DOWN:
        return Verdict("down", "Last run failed",
                       reasons + [f"Last run status: {last_status}.",
                                  *( [last_err] if last_err else [] )])

    if last_status in RUN_STATUS_WARN:
        return Verdict("degraded", "Last run warned",
                       reasons + [f"Last run status: {last_status}."])

    if reasons:
        return Verdict("degraded", "Degraded heartbeat", reasons)

    return Verdict("healthy", "Healthy", [f"Heartbeat {int(age_min)}m ago."])


# ── State (de-dup pings across runs) ─────────────────────────────────────────


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[league_health] state read failed: {e!r}")
    return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as e:  # noqa: BLE001
        print(f"[league_health] state write failed: {e!r}")


# ── Discord ──────────────────────────────────────────────────────────────────


def discord_ping(content: str, embed: Optional[dict] = None) -> None:
    webhook = os.getenv("LEAGUE_DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[league_health] LEAGUE_DISCORD_WEBHOOK_URL not set; skipping ping")
        return
    payload: dict[str, Any] = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = requests.post(webhook, json=payload, timeout=TIMEOUT_DISCORD)
        if resp.status_code >= 400:
            print(f"[league_health] discord status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[league_health] discord post failed: {e!r}")


_COLOR = {
    "healthy":  0x16A34A,  # green
    "degraded": 0xD97706,  # amber
    "down":     0xDC2626,  # red
    "idle":     0x737373,  # grey
}


def embed_for(view: BotView, verdict: Verdict) -> dict:
    s = view.status_row or {}
    fields = [
        {"name": "Bot",   "value": view.display_name, "inline": True},
        {"name": "Mode",  "value": view.mode or "—",  "inline": True},
        {"name": "State", "value": verdict.label,     "inline": True},
    ]
    if s.get("last_heartbeat_at"):
        fields.append({"name": "Last heartbeat", "value": str(s["last_heartbeat_at"]), "inline": False})
    if s.get("last_run_status"):
        fields.append({"name": "Last run", "value": str(s["last_run_status"]), "inline": True})
    return {
        "title":       f"League: {view.bot_id}",
        "description": "\n".join(verdict.reasons) or "—",
        "color":       _COLOR.get(verdict.bucket, 0x000000),
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


# ── Main ─────────────────────────────────────────────────────────────────────


def should_ping(prev: dict[str, Any], bot_id: str, bucket: str) -> bool:
    """Ping on transition into a bad state, or every COOLDOWN_HOURS while bad."""
    prev_entry = prev.get(bot_id) or {}
    prev_bucket = prev_entry.get("bucket") or "healthy"
    last_ping = prev_entry.get("last_ping_ts")  # unix seconds

    # Transitions: any change is worth a ping (good or bad)
    if bucket != prev_bucket:
        return True

    # Stuck bad: re-ping every COOLDOWN_HOURS
    if bucket in ("degraded", "down"):
        if last_ping is None:
            return True
        if time.time() - float(last_ping) >= COOLDOWN_HOURS * 3600:
            return True

    return False


def main() -> int:
    cfg = _config()
    if cfg is None:
        print("[league_health] LEAGUE_SUPABASE_URL / LEAGUE_SUPABASE_KEY missing — exit 0")
        return 0

    views = fetch_views(cfg)
    if not views:
        print("[league_health] no bots in registry; nothing to do")
        return 0

    prev = load_state()
    new_state: dict[str, Any] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    summary_lines: list[str] = []
    transitions: list[tuple[BotView, Verdict, str]] = []  # (view, verdict, prev_bucket)

    for v in views:
        verdict = derive(v)
        prev_entry = prev.get(v.bot_id) or {}
        prev_bucket = prev_entry.get("bucket") or "healthy"

        new_state[v.bot_id] = {
            "bucket":     verdict.bucket,
            "label":      verdict.label,
            "updated_at": now_iso,
            "last_ping_ts": prev_entry.get("last_ping_ts"),
        }

        summary_lines.append(
            f"  {v.bot_id:<24} mode={v.mode:<8} -> {verdict.bucket:<8} ({verdict.label})"
        )

        if should_ping(prev, v.bot_id, verdict.bucket):
            transitions.append((v, verdict, prev_bucket))
            new_state[v.bot_id]["last_ping_ts"] = time.time()

    print("[league_health] survey:")
    for line in summary_lines:
        print(line)

    # Send pings (one Discord message per affected bot — keeps the channel scannable)
    if transitions:
        print(f"[league_health] {len(transitions)} ping(s) to send")
        for v, verdict, prev_bucket in transitions:
            arrow = f"{prev_bucket} → {verdict.bucket}"
            label = {
                "healthy":  "✓",
                "degraded": "⚠",
                "down":     "✗",
                "idle":     "·",
            }.get(verdict.bucket, "?")
            content = f"{label} **{v.display_name}** ({v.bot_id}): {arrow}"
            discord_ping(content, embed=embed_for(v, verdict))
    else:
        print("[league_health] no transitions; no pings")

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
