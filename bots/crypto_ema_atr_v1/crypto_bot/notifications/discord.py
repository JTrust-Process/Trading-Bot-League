# crypto_bot/notifications/discord.py
#
# Fix from audit:
#   - Issue 8: webhook send now retries on HTTP 429 with Retry-After backoff

import os
import time
import requests
from datetime import datetime, timezone

_EVENTS = {
    "buy":           ("🟢", 0x1a6b3c, "BUY ORDER PLACED"),
    "sell_profit":   ("💰", 0x1a6b3c, "SELL — PROFIT"),
    "sell_loss":     ("🔴", 0xc8391a, "SELL — LOSS"),
    "stop_loss":     ("🛑", 0xc8391a, "STOP LOSS TRIGGERED"),
    "take_profit":   ("🎯", 0x1a6b3c, "TAKE PROFIT TRIGGERED"),
    "circuit_break": ("⚡", 0x92600a, "CIRCUIT BREAKER TRIPPED"),
    "error":         ("❌", 0xc8391a, "BOT ERROR"),
    "daily_summary": ("📊", 0x1a3a6b, "DAILY SUMMARY"),
}


def _get_webhook_url() -> str | None:
    return os.getenv("DISCORD_WEBHOOK_URL")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _post_with_retry(url: str, payload: dict, max_attempts: int = 3) -> None:
    """
    POST to webhook with retry on rate limits.
    Issue 8: Discord returns 429 + Retry-After header when rate-limited.
    Without this, simultaneous BUY+SELL+daily summary in one cycle could
    silently drop messages.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                # Discord returns ms in some cases via JSON body
                try:
                    body = resp.json()
                    retry_after = max(retry_after, float(body.get("retry_after", 0)))
                except Exception:
                    pass
                if attempt < max_attempts:
                    print(f"[discord] Rate limited — sleeping {retry_after:.1f}s")
                    time.sleep(retry_after)
                    continue
            print(f"[discord] Webhook returned {resp.status_code}: {resp.text[:200]}")
            return
        except Exception as e:
            if attempt < max_attempts:
                time.sleep(2)
            else:
                print(f"[discord] Failed to send notification: {e}")


def send(event: str, title: str, fields: dict, description: str = "") -> None:
    """Send a rich embed to Discord. Never raises — silently logs on failure."""
    url = _get_webhook_url()
    if not url:
        return

    emoji, color, _ = _EVENTS.get(event, ("📊", 0x3a3832, event.upper()))

    embed = {
        "title": f"{emoji}  {title}",
        "color": color,
        "fields": [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()],
        "footer": {"text": f"Crypto Bot  ·  {_now()}"},
    }
    if description:
        embed["description"] = description

    _post_with_retry(url, {"embeds": [embed]})


# ── Convenience wrappers ──────────────────────────────────────────────────────

def notify_buy(symbol: str, price: float, usd_amount: float, sl: float, tp: float) -> None:
    send("buy", f"Buy {symbol}", {
        "Symbol":      symbol,
        "Price":       f"${price:,.2f}",
        "Amount":      f"${usd_amount:.2f}",
        "Stop Loss":   f"${sl:,.2f}",
        "Take Profit": f"${tp:,.2f}",
    })


def notify_sell(symbol: str, price: float, pnl: float, reason: str) -> None:
    event = (
        "take_profit" if reason == "TAKE_PROFIT" else
        "stop_loss"   if reason == "STOP_LOSS"   else
        ("sell_profit" if pnl >= 0 else "sell_loss")
    )
    send(event, f"Sell {symbol} — {reason}", {
        "Symbol": symbol,
        "Price":  f"${price:,.2f}",
        "P&L":    f"{'+'if pnl>=0 else ''}{pnl:.4f} USD",
        "Reason": reason,
    })


def notify_circuit_breaker(symbol: str, consecutive_losses: int) -> None:
    send("circuit_break", "Circuit Breaker Tripped", {
        "Symbol":             symbol,
        "Consecutive Losses": consecutive_losses,
        "Action":             "Trading paused for this symbol",
    }, description="Too many consecutive losses. Will resume when streak resets.")


def notify_error(context: str, error: str) -> None:
    send("error", "Bot Error", {
        "Context": context,
        "Error":   error[:200],
    })


# ── Daily summary ─────────────────────────────────────────────────────────────

def should_send_daily_summary(state: dict) -> bool:
    return state.get("last_daily_summary", "") != _today_key()


def notify_daily_summary(
    state: dict,
    symbols: list,
    price_history: dict,
    dry_run: bool,
) -> None:
    capital   = state.get("capital", 0)
    positions = state.get("positions", {})
    losses    = state.get("consecutive_losses", {})

    fields = {
        "Capital":   f"${capital:.2f}",
        "Mode":      "DRY RUN" if dry_run else "LIVE",
        "Positions": ", ".join(positions.keys()) if positions else "None",
    }

    # Audit L6: reuse the canonical EMA helper from signal.py so the gap
    # shown in the daily summary matches what the trading logic computes.
    # Previously this seeded from prices[0]; signal.py uses an SMA seed.
    from crypto_bot.strategy.signal import ema as _ema
    from crypto_bot.config.settings import EMA_FAST, EMA_SLOW

    for sym in symbols:
        prices = price_history.get(sym, [])
        if len(prices) >= EMA_SLOW + 1:
            ema_fast = _ema(prices, EMA_FAST)
            ema_slow = _ema(prices, EMA_SLOW)
            gap = ema_fast[-1] - ema_slow[-1]
            direction = "↑ bullish" if gap > 0 else "↓ bearish"
            fields[f"{sym} EMA gap"] = f"{gap:+.2f} ({direction})"
        else:
            fields[f"{sym} EMA"] = f"Warming up ({len(prices)}/{EMA_SLOW + 1})"

        if prices:
            fields[f"{sym} Price"] = f"${prices[-1]:,.2f}"

        l = losses.get(sym, 0)
        if l > 0:
            fields[f"{sym} Loss streak"] = f"{l} ⚠️"

    for sym, pos in positions.items():
        fields[f"{sym} Entry"] = f"${pos['entry']:,.2f}"
        fields[f"{sym} SL/TP"] = f"${pos['stop_loss']:,.2f} / ${pos['take_profit']:,.2f}"

    send("daily_summary", "Daily Summary", fields,
         description=f"24-hour bot status as of {_now()}")

    state["last_daily_summary"] = _today_key()