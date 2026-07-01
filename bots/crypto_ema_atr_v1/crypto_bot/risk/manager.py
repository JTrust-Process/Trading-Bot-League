# crypto_bot/risk/manager.py
#
# Issue 2 fix: position_size_usd reads capital LIVE from state instead of the
# snapshot passed in at startup. So if multiple trades close in the same run,
# each new buy sizes from the updated capital, not the stale pre-run value.
#
# Audit C3: position size now divides by the *actual* stop distance for this
# trade (ATR-derived in the normal path, falling back to STOP_LOSS_PCT when
# ATR is unavailable). Previously this always divided by STOP_LOSS_PCT, which
# under-counted risk whenever the real ATR-based stop was wider — actual risk
# per trade was ~50%+ above the configured RISK_PER_TRADE on volatile days.

from crypto_bot.config.settings import RISK_PER_TRADE, STOP_LOSS_PCT, get_max_order_usd


class RiskManager:
    def __init__(self, state: dict):
        """
        Holds a reference to the state dict so capital reads stay live.
        """
        self.state = state

    @property
    def capital(self) -> float:
        return self.state.get("capital", 0.0)

    def position_size_usd(self, price: float, stop_pct: float | None = None) -> float:
        """
        Returns USD notional amount to spend on this trade.

        Formula: (capital * risk_pct) / actual_stop_pct
        Hard capped at MAX_ORDER_AMOUNT_USD so a growing account never places
        oversized orders. Also clamped so it never exceeds available capital.

        Args:
            price:    current quote (kept in signature for caller ergonomics —
                      not used in the core formula but useful for future
                      tick-rounding logic)
            stop_pct: actual stop distance as a fraction (e.g. 0.045 for 4.5%).
                      Pass the ATR-derived stop here. Falls back to the
                      hardcoded STOP_LOSS_PCT if None / non-positive.

        Example at $50 capital, 2% risk, 4.5% real stop, $25 cap:
          risk_amount  = $1.00
          usd_to_spend = 1.00 / 0.045 = $22.22  → under cap, used as is
        """
        if stop_pct is None or stop_pct <= 0:
            stop_pct = STOP_LOSS_PCT

        # Floor stop_pct so a tiny ATR doesn't blow up the divisor and
        # produce an absurdly large notional. Anything <0.5% is suspect.
        stop_pct = max(stop_pct, 0.005)

        capital     = self.capital
        risk_amount = capital * RISK_PER_TRADE
        usd_amount  = risk_amount / stop_pct
        usd_amount  = min(usd_amount, capital)              # never exceed capital
        usd_amount  = min(usd_amount, get_max_order_usd())  # hard cap
        usd_amount  = max(usd_amount, 1.0)                  # minimum viable
        return round(usd_amount, 2)