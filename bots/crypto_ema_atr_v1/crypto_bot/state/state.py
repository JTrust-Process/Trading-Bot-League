# crypto_bot/state/state.py

import json
import os
from datetime import datetime, timezone
from crypto_bot.config.settings import STATE_FILE, PRICE_HISTORY_SIZE, get_initial_capital


def _default_state() -> dict:
    # Audit L3: pull capital from env (via get_initial_capital) instead of the
    # module-level constant so INITIAL_CAPITAL in .env / workflow actually works.
    return {
        "capital":            get_initial_capital(),
        "positions":          {},
        "price_history":      {},
        "consecutive_losses": {},
        "circuit_notified":   {},
        "last_daily_summary": "",
        # Cooldown: candle index at which a position last closed per symbol
        "last_exit_candle":   {},
        # Reconciliation: symbols where Public has crypto but state doesn't.
        # When True, BUYs for that symbol are blocked.
        "position_desync":    {},
        # M3: per-symbol UTC-day token to rate-limit "ATR fallback in effect" alerts
        "atr_fallback_notified": {},
    }


def load_state() -> dict:
    # H1: try Supabase fallback only if local state.json is absent or unreadable.
    # Local cache (GHA actions/cache) is the primary; Supabase is the safety net
    # for cache misses / evictions. The import is local to avoid an import cycle
    # (state.py is imported by engine, which is imported by main, which loads
    # dotenv before settings reads env vars).
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            return _normalise(data)
        except (json.JSONDecodeError, OSError):
            pass

    try:
        from crypto_bot.state.remote import load_state_from_supabase
        remote = load_state_from_supabase()
        if remote is not None:
            print("[state] state.json missing/unreadable — restored from Supabase")
            return _normalise(remote)
    except Exception as e:
        print(f"[state] Supabase fallback failed: {e}")

    return _default_state()


def _normalise(data: dict) -> dict:
    """Apply default keys to a loaded state dict so downstream code can rely
    on every key being present."""
    data.setdefault("capital",            get_initial_capital())
    data.setdefault("positions",          {})
    data.setdefault("price_history",      {})
    data.setdefault("consecutive_losses", {})
    data.setdefault("circuit_notified",   {})
    data.setdefault("last_daily_summary", "")
    data.setdefault("last_exit_candle",   {})
    data.setdefault("position_desync",    {})
    data.setdefault("atr_fallback_notified", {})
    return data


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

    # H1: also mirror to Supabase as a recovery net for cache misses /
    # eviction. Best-effort — failures here don't impact the local save.
    try:
        from crypto_bot.state.remote import save_state_to_supabase
        save_state_to_supabase(state)
    except Exception as e:
        print(f"[state] Supabase mirror failed: {e}")


def append_price(state: dict, symbol: str, price: float) -> None:
    history = state.setdefault("price_history", {}).setdefault(symbol, [])
    history.append(price)
    if len(history) > PRICE_HISTORY_SIZE:
        state["price_history"][symbol] = history[-PRICE_HISTORY_SIZE:]


def get_price_history(state: dict, symbol: str) -> list:
    return state.get("price_history", {}).get(symbol, [])


def record_loss(state: dict, symbol: str) -> int:
    losses = state.setdefault("consecutive_losses", {})
    losses[symbol] = losses.get(symbol, 0) + 1
    return losses[symbol]


def record_win(state: dict, symbol: str) -> None:
    state.setdefault("consecutive_losses", {})[symbol] = 0
    state.setdefault("circuit_notified", {}).pop(symbol, None)


def get_consecutive_losses(state: dict, symbol: str) -> int:
    return state.get("consecutive_losses", {}).get(symbol, 0)


def is_circuit_broken(state: dict, symbol: str, threshold: int) -> bool:
    return get_consecutive_losses(state, symbol) >= threshold


def should_notify_circuit_broken(state: dict, symbol: str) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.setdefault("circuit_notified", {}).get(symbol, "") != today


def mark_circuit_notified(state: dict, symbol: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.setdefault("circuit_notified", {})[symbol] = today


# ── Cooldown tracking (NEW) ───────────────────────────────────────────────────

def record_exit(state: dict, symbol: str) -> None:
    """
    Record the candle index at which a position was closed.
    Used to enforce a cooldown before re-entering the same symbol.
    """
    current_candle = len(state.get("price_history", {}).get(symbol, []))
    state.setdefault("last_exit_candle", {})[symbol] = current_candle


def candles_since_exit(state: dict, symbol: str) -> int | None:
    """
    Returns number of candles elapsed since last exit on this symbol.
    Returns None if no prior exit recorded (no cooldown applies).
    """
    last_exit = state.get("last_exit_candle", {}).get(symbol)
    if last_exit is None:
        return None
    current = len(state.get("price_history", {}).get(symbol, []))
    return current - last_exit


# ── Position desync tracking (NEW — startup reconciliation) ──────────────────
# When the bot loads state and finds Public has crypto but state.json doesn't,
# we mark the symbol as desynced and block all BUYs until manually resolved.

def mark_position_desync(state: dict, symbol: str, real_qty: float) -> None:
    """Flag a symbol as having a Public position not reflected in local state."""
    state.setdefault("position_desync", {})[symbol] = {
        "real_qty":   real_qty,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def is_position_desynced(state: dict, symbol: str) -> bool:
    return symbol in state.get("position_desync", {})


def clear_position_desync(state: dict, symbol: str) -> None:
    """Call when desync is resolved (e.g. position manually closed on Public)."""
    state.setdefault("position_desync", {}).pop(symbol, None)


# ── ATR-fallback rate-limited alerting (M3) ───────────────────────────────────

def should_notify_atr_fallback(state: dict, symbol: str) -> bool:
    """Return True at most once per UTC day per symbol so a persistent
    CoinGecko outage doesn't spam Discord."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.setdefault("atr_fallback_notified", {}).get(symbol, "") != today


def mark_atr_fallback_notified(state: dict, symbol: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.setdefault("atr_fallback_notified", {})[symbol] = today