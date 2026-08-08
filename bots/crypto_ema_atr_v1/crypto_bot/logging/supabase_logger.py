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
    # Guard against corrupt writes (added 2026-08-08).
    #
    # A row exists in crypto_trades dated 2026-04-15 with BOTH a negative
    # price (-2339.84) and a negative size (-0.00427380) on an ETH entry.
    # A negative price is not a thing; a negative size on a BUY is not a
    # thing. Something upstream — most likely a signed quantity from a
    # partially-parsed API response — produced it, and it was written
    # without complaint because this function coerces to float and inserts.
    #
    # That row then silently corrupted every aggregate computed over this
    # table: sum(pnl), trade counts, average size. It was found only by
    # reading the raw history line by line.
    #
    # We refuse the write rather than sanitising it. A trade we cannot
    # describe correctly is worse than a trade we did not log — the missing
    # row is visible in reconciliation against the broker, whereas a
    # plausible-looking wrong row is not.
    if price is None or float(price) <= 0:
        print(f"[supabase_logger] REFUSING trade write: {symbol} {side} has "
              f"non-positive price {price!r}. Not logged. Reconcile against "
              f"Public if this trade actually executed.")
        return
    if size is None or float(size) <= 0:
        print(f"[supabase_logger] REFUSING trade write: {symbol} {side} has "
              f"non-positive size {size!r}. Not logged. Reconcile against "
              f"Public if this trade actually executed.")
        return

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