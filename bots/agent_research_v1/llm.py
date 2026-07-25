"""bots/agent_research_v1/llm.py — Anthropic API client (plain requests).

Why we don't pip-install `anthropic`: keeps the bot's dependency footprint
identical to every other League bot (just `requests` + `python-dotenv`).
The /v1/messages endpoint is straightforward enough that a 60-line HTTP
wrapper is preferable to pinning yet another package.

Failure mode: every error path returns None. Callers must handle "the LLM
didn't give us anything usable" without crashing. The bot's cycle still
publishes a heartbeat and run row even when the model is unavailable —
just no brief and no proposals that day.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import requests


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Token usage from the most recent call() — populated on success, reset to
# {} on failure. Read by main.py so the cycle can record what it actually
# spent instead of relying on an estimate.
#
# Added 2026-07-25. Until now the `usage` block Anthropic returns on every
# response was discarded, which meant this bot's cost was never measured.
# bot_expenses_seed.sql asserts "~$0.003/run x ~250 weekday fires" as though
# it were known; it was a guess, and it was ~2x low anyway because the bot
# ran on BOTH GHA and Fly from the 2026-07-24 migration until the duplicate
# crons were disabled on 2026-07-25.
LAST_USAGE: Dict[str, Any] = {}

# Default model — Haiku is cheap (~$0.003 per daily run on this size of
# context) and plenty capable for structured summarization. Override via
# AGENT_MODEL env var if you want Sonnet / Opus.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TIMEOUT = 30.0


def call(
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Send a single-turn message to Claude. Returns the assistant's text,
    or None on any error. Fail-silent.

    Side effect: populates module-level LAST_USAGE with the token counts
    from this call (cleared first, so a failed call leaves it empty rather
    than stale from a previous run).
    """
    LAST_USAGE.clear()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[agent.llm] ANTHROPIC_API_KEY not set; skipping LLM call")
        return None

    payload: Dict[str, Any] = {
        "model":      model or os.getenv("AGENT_MODEL", DEFAULT_MODEL),
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type":      "application/json",
    }

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers=headers,
            data=json.dumps(payload),
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[agent.llm] POST failed: {e!r}")
        return None

    if resp.status_code >= 400:
        print(f"[agent.llm] status={resp.status_code} body={resp.text[:300]}")
        return None

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[agent.llm] json decode failed: {e!r}")
        return None

    # Record what this call actually consumed. Anthropic returns e.g.
    # {"input_tokens": 1234, "output_tokens": 567}. We deliberately do NOT
    # convert to dollars here — per-token pricing is not something this
    # module can know reliably, and a stale hardcoded rate would just
    # recreate the "confident number with nothing behind it" problem this
    # is meant to fix. Set AGENT_COST_PER_MTOK_IN / _OUT (dollars per
    # MILLION tokens) if you want an estimated_cost_usd computed; leave
    # them unset and only the raw token counts are recorded.
    usage = data.get("usage")
    if isinstance(usage, dict):
        LAST_USAGE.update({
            "model":         payload["model"],
            "input_tokens":  usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        })
        cost = _estimate_cost_usd(
            LAST_USAGE.get("input_tokens"),
            LAST_USAGE.get("output_tokens"),
        )
        if cost is not None:
            LAST_USAGE["estimated_cost_usd"] = cost
        print(f"[agent.llm] usage model={LAST_USAGE['model']} "
              f"in={LAST_USAGE.get('input_tokens')} "
              f"out={LAST_USAGE.get('output_tokens')}"
              + (f" est=${cost:.5f}" if cost is not None else ""))

    # Anthropic returns content as a list of blocks; text blocks have type='text'.
    blocks = data.get("content") or []
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        print("[agent.llm] empty assistant text in response")
        return None
    return text


def _estimate_cost_usd(in_tokens: Any, out_tokens: Any) -> Optional[float]:
    """Estimate spend from token counts, but ONLY when rates are configured.

    Returns None unless both AGENT_COST_PER_MTOK_IN and
    AGENT_COST_PER_MTOK_OUT are set (dollars per million tokens). Keeping
    this opt-in means we never publish a cost figure derived from a price
    we guessed — check Anthropic's current pricing and set the env vars if
    you want dollars alongside the token counts.
    """
    try:
        rate_in = float(os.getenv("AGENT_COST_PER_MTOK_IN", "") or "")
        rate_out = float(os.getenv("AGENT_COST_PER_MTOK_OUT", "") or "")
        n_in = float(in_tokens or 0)
        n_out = float(out_tokens or 0)
    except (TypeError, ValueError):
        return None
    return (n_in / 1_000_000.0) * rate_in + (n_out / 1_000_000.0) * rate_out


def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Robustly pull a JSON object out of an LLM response.

    Handles three cases:
      1. The whole response is valid JSON.
      2. A ```json ... ``` fenced block is present.
      3. The first {...} that parses cleanly is what we want.
    Returns the parsed dict, or None if nothing parseable is found.
    """
    if not text:
        return None

    # Case 1: whole thing parses.
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass

    # Case 2: fenced ```json ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass

    # Case 3: greedy first balanced {...} block — scan for the first '{' and
    # try expanding until we find a JSON-parseable substring.
    start = s.find("{")
    if start == -1:
        return None
    # Try progressively longer slices ending at each subsequent '}'.
    end = start
    while True:
        end = s.find("}", end + 1)
        if end == -1:
            return None
        try:
            obj = json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            continue


__all__ = ["call", "extract_json_block", "DEFAULT_MODEL", "LAST_USAGE"]
