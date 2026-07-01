# crypto_bot/logging/supabase_logger.py

import os

from crypto_bot.logging._supabase import safe_insert, now_iso
from crypto_bot.league import league_status  # ADDITIVE — fail-silent trade mirror to League


def log_trade(
    symbol: str,
    side: str,
    price: float,
    size: float,
    pnl: float = 0.0,
    reason: str = "",
    run_id: str | None = None,
    order_id: str | None = None,  # Public's order_id for reconciliation
) -> None:
    """Insert a trade record into crypto_trades. Never raises."""
    data = {
        "run_id":    run_id,
        "timestamp": now_iso(),
        "symbol":    symbol,
        "side":      side.upper(),
        "price":     float(price),
        "size":      float(size),
        "pnl":       float(pnl),
        "reason":    reason,
    }
    if order_id:
        data["order_id"] = order_id
    safe_insert("crypto_trades", data)

    # League mirror — additive, fail-silent. DRY_RUN=1 marks the trade as
    # paper so the leaderboard can separate live vs paper performance.
    # Per the crypto bot's README, DRY_RUN trades also get reason prefixed
    # "DRY_RUN/" — we honor that as a second source of truth.
    try:
        is_paper = (os.getenv("DRY_RUN", "0") == "1") or str(reason or "").startswith("DRY_RUN/")
        league_status.log_trade(
            symbol=symbol,
            side=side,
            quantity=float(size),
            price=float(price),
            pnl_usd=float(pnl),
            reason=reason or None,
            strategy="ema_atr_v1",
            is_paper=is_paper,
            order_id=order_id,
            run_id=run_id,
        )
    except Exception:
        pass