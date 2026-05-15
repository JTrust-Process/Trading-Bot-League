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
    or None on any error. Fail-silent."""
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

    # Anthropic returns content as a list of blocks; text blocks have type='text'.
    blocks = data.get("content") or []
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        print("[agent.llm] empty assistant text in response")
        return None
    return text


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


__all__ = ["call", "extract_json_block", "DEFAULT_MODEL"]
