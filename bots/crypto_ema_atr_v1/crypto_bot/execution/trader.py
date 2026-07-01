# crypto_bot/execution/trader.py

import time
from crypto_bot.exchange.public_api import (
    get_primary_account_id,
    place_order_buy,
    place_order_sell,
    get_order,
    get_cash_buying_power,
    get_crypto_position_quantity,
)
from crypto_bot.logging import supabase_logger
from crypto_bot.logging.logger import log, log_error, log_warn
from crypto_bot.notifications import discord
from crypto_bot.state.state import record_win, record_loss, record_exit
from crypto_bot.config.settings import (
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, is_dry_run, get_min_buying_power_buffer,
    get_atr_sl_multiplier, get_atr_tp_multiplier, get_crypto_fee_per_order,
)
from crypto_bot.data.coingecko import compute_atr

FILL_POLL_ATTEMPTS = 8
FILL_POLL_DELAY    = 2.5


def _poll_fill_price(account_id: str, order_id: str, fallback_price: float) -> tuple[float, float]:
    for attempt in range(1, FILL_POLL_ATTEMPTS + 1):
        try:
            order = get_order(account_id, order_id)
            status     = order.get("status", "")
            avg_price  = order.get("averagePrice")
            filled_qty = order.get("filledQuantity")

            if status == "FILLED" and avg_price:
                return float(avg_price), float(filled_qty or 0)

            if status in ("CANCELLED", "REJECTED", "EXPIRED"):
                log_warn(f"[trader] Order {order_id} ended with status {status}")
                return fallback_price, 0.0

            if attempt < FILL_POLL_ATTEMPTS:
                time.sleep(FILL_POLL_DELAY)

        except Exception as e:
            if attempt < FILL_POLL_ATTEMPTS:
                time.sleep(FILL_POLL_DELAY)
            else:
                log_warn(f"[trader] Could not confirm fill for {order_id}: {e} — using quote price")

    return fallback_price, 0.0


def compute_stop_levels(symbol: str, entry: float, run_id: str | None) -> tuple[float, float, str]:
    """
    Calculate stop loss and take profit levels for a hypothetical entry at
    `entry`. Used by both the engine (BEFORE position sizing — audit C3) and
    by trader.buy() if a pre-computed value isn't supplied.

    Tries ATR-based scaling first (volatility-adaptive). If ATR fetch fails,
    falls back to fixed percentages from settings.

    Returns (stop_loss, take_profit, method_used).  method_used is "ATR" or "FIXED".
    """
    try:
        atr = compute_atr(symbol, period=14)
        if atr is not None and atr > 0:
            sl_mult = get_atr_sl_multiplier()
            tp_mult = get_atr_tp_multiplier()
            sl = entry - (sl_mult * atr)
            tp = entry + (tp_mult * atr)
            sl_pct = ((entry - sl) / entry) * 100
            tp_pct = ((tp - entry) / entry) * 100
            log(
                f"[trader] {symbol} ATR={atr:.2f} → SL={sl:.2f} (-{sl_pct:.2f}%) "
                f"TP={tp:.2f} (+{tp_pct:.2f}%)",
                run_id,
            )
            return round(sl, 6), round(tp, 6), "ATR"
    except Exception as e:
        log_warn(f"[trader] ATR calc failed for {symbol}: {e} — falling back to fixed %", run_id)

    # Fallback: fixed percentages
    sl = round(entry * (1 - STOP_LOSS_PCT), 6)
    tp = round(entry * (1 + TAKE_PROFIT_PCT), 6)
    log(f"[trader] {symbol} using fixed SL={sl:.2f} TP={tp:.2f}", run_id)
    return sl, tp, "FIXED"


# Backwards-compatible alias — kept so any external callers (research scripts,
# notebooks) that imported the old name don't break.
_calculate_sl_tp = compute_stop_levels


def stop_pct_from_levels(entry: float, stop_loss: float) -> float:
    """Helper: return |entry - stop_loss| / entry as a fraction.
    Used by RiskManager.position_size_usd for accurate sizing."""
    if entry <= 0:
        return 0.0
    return max(0.0, (entry - stop_loss) / entry)


class Trader:
    def __init__(self, state: dict, run_id: str | None = None):
        self.state       = state
        self.run_id      = run_id
        self._account_id = None
        self._dry_run    = is_dry_run()
        if self._dry_run:
            log("DRY RUN mode enabled — orders will be simulated, not placed", run_id)

    @property
    def positions(self) -> dict:
        return self.state.setdefault("positions", {})

    def _get_account_id(self) -> str:
        if self._account_id is None:
            self._account_id = get_primary_account_id()
        return self._account_id

    # ── BUY ────────────────────────────────────────────────────────────────────
    def buy(
        self,
        symbol: str,
        price: float,
        usd_amount: float,
        sl_tp: tuple[float, float, str] | None = None,
    ) -> None:
        """
        Audit C3: callers may now pass pre-computed (sl, tp, method) so the
        same ATR result that drove sizing is also used as the stop/target —
        avoids double-computation and a possible drift between sizing and
        stop placement if ATR changed between calls.
        """
        fill_price = price
        actual_qty: float | None = None
        order_id:   str | None   = None  # captured from API response, saved to Supabase

        if self._dry_run:
            log(f"[DRY RUN] Would BUY {symbol} ${usd_amount:.2f}", self.run_id)
        else:
            try:
                cash_bp = get_cash_buying_power(self._get_account_id())
                buffer  = get_min_buying_power_buffer()
                if cash_bp - usd_amount < buffer:
                    log_warn(
                        f"[trader] BUY {symbol} skipped — buying power ${cash_bp:.2f} "
                        f"would drop below ${buffer:.2f} buffer after ${usd_amount:.2f} order",
                        self.run_id,
                    )
                    return
                log(
                    f"[trader] Buying power check passed — ${cash_bp:.2f} available, "
                    f"need ${usd_amount:.2f} + ${buffer:.2f} buffer",
                    self.run_id,
                )
            except Exception as e:
                # FAIL CLOSED — if we can't verify buying power, don't trade.
                # Old behavior was to proceed; audit flagged this as risky.
                # Better to skip a trade than to over-leverage during an API outage.
                log_error(
                    f"[trader] BUY {symbol} skipped — buying power check failed: {e}",
                    self.run_id,
                )
                discord.notify_error(
                    f"BUY {symbol}",
                    f"Buying power check failed: {e}",
                )
                return

            try:
                result   = place_order_buy(self._get_account_id(), symbol, usd_amount)
                order_id = result.get("orderId") or result.get("_clientOrderId")

                if order_id:
                    raw_fill, raw_qty = _poll_fill_price(self._get_account_id(), order_id, price)
                    if raw_fill > 0:
                        fill_price = raw_fill
                        if fill_price != price:
                            log(
                                f"[trader] BUY {symbol} fill price {fill_price:.2f} (quote was {price:.2f})",
                                self.run_id,
                            )
                    else:
                        log_warn(
                            f"[trader] BUY {symbol} fill price from API was invalid ({raw_fill:.2f}) "
                            f"— using quote price {price:.2f}",
                            self.run_id,
                        )
                        fill_price = price
                    if raw_qty > 0:
                        actual_qty = raw_qty

            except Exception as e:
                log_error(f"[trader] BUY {symbol} failed: {e}", self.run_id)
                discord.notify_error(f"BUY {symbol}", str(e))
                return

        size_estimate = actual_qty if actual_qty else (usd_amount / fill_price)

        # ATR-based SL/TP (with fallback to fixed %).
        # If the engine pre-computed (to drive position sizing), reuse those
        # values rather than calling compute_atr() a second time.
        if sl_tp is not None:
            sl, tp, method = sl_tp
        else:
            sl, tp, method = compute_stop_levels(symbol, fill_price, self.run_id)

        candles_at_entry = len(self.state.get("price_history", {}).get(symbol, []))

        self.positions[symbol] = {
            "entry":            fill_price,
            "usd_amount":       usd_amount,
            "size_estimate":    size_estimate,
            "stop_loss":        sl,
            "take_profit":      tp,
            "candles_at_entry": candles_at_entry,
            "entry_candle":     candles_at_entry,  # legacy compat
            "exit_method":      method,            # "ATR" or "FIXED" — for analysis
        }

        mode = "DRY RUN " if self._dry_run else ""
        log(
            f"{mode}BUY  {symbol} | fill={fill_price:.2f} | amount=${usd_amount:.2f} | "
            f"qty={size_estimate:.8f} | SL={sl:.2f} TP={tp:.2f} ({method})",
            self.run_id,
        )

        reason = "DRY_RUN/entry" if self._dry_run else "entry"
        supabase_logger.log_trade(symbol, "BUY", fill_price, size_estimate, 0.0, reason, self.run_id, order_id=order_id)

        if not self._dry_run:
            discord.notify_buy(symbol, fill_price, usd_amount, sl, tp)

    # ── SELL ───────────────────────────────────────────────────────────────────
    def sell(self, symbol: str, price: float, reason: str = "signal") -> None:
        if symbol not in self.positions:
            log_error(f"[trader] sell called for {symbol} but no open position", self.run_id)
            return

        pos           = self.positions[symbol]
        entry_price   = pos["entry"]
        usd_amount    = pos["usd_amount"]
        size_estimate = pos.get("size_estimate", 0)

        if not size_estimate or size_estimate <= 0:
            size_estimate = usd_amount / entry_price
            log_warn(
                f"[trader] {symbol} size_estimate was 0 — recalculated as {size_estimate:.8f} from entry",
                self.run_id,
            )

        fill_price = price
        order_id: str | None = None  # captured from API response, saved to Supabase

        if self._dry_run:
            log(f"[DRY RUN] Would SELL {symbol} qty={size_estimate:.8f}", self.run_id)
        else:
            try:
                real_qty = get_crypto_position_quantity(self._get_account_id(), symbol)
                if real_qty is not None and real_qty > 0:
                    if size_estimate > 0 and abs(real_qty - size_estimate) / size_estimate > 0.01:
                        log_warn(
                            f"[trader] {symbol} size_estimate {size_estimate:.8f} differs from "
                            f"actual position {real_qty:.8f} — using real quantity",
                            self.run_id,
                        )
                    size_estimate = real_qty
                else:
                    log_warn(
                        f"[trader] Could not fetch real {symbol} quantity — using estimate {size_estimate:.8f}",
                        self.run_id,
                    )
            except Exception as e:
                log_warn(f"[trader] Portfolio quantity check failed: {e} — using estimate", self.run_id)

            try:
                result   = place_order_sell(self._get_account_id(), symbol, size_estimate)
                order_id = result.get("orderId") or result.get("_clientOrderId")

                if order_id:
                    raw_fill, filled_qty = _poll_fill_price(self._get_account_id(), order_id, price)
                    if raw_fill > 0:
                        fill_price = raw_fill
                        if fill_price != price:
                            log(
                                f"[trader] SELL {symbol} fill price {fill_price:.2f} (quote was {price:.2f})",
                                self.run_id,
                            )
                    else:
                        log_warn(
                            f"[trader] SELL {symbol} fill price from API was invalid ({raw_fill:.2f}) "
                            f"— using quote price {price:.2f}",
                            self.run_id,
                        )
                        fill_price = price
                    if filled_qty > 0:
                        size_estimate = filled_qty

                # ─── Audit C2: partial-fill protection ───────────────────────
                # Re-query post-trade quantity from Public. If anything is
                # still on the books for this symbol, the SELL was partial.
                # We compute PnL on what *did* fill and KEEP the position
                # with the remaining size so the next cycle can finish it
                # (or hit SL/TP normally). Without this guard the bot would
                # delete the local position while Public still held crypto,
                # producing wrong PnL and a permanent DESYNC flag.
                try:
                    remaining = get_crypto_position_quantity(self._get_account_id(), symbol)
                except Exception as e:
                    remaining = None
                    log_warn(
                        f"[trader] SELL {symbol} post-trade quantity check failed: {e} "
                        f"— assuming full fill",
                        self.run_id,
                    )

            except Exception as e:
                log_error(f"[trader] SELL {symbol} failed: {e}", self.run_id)
                discord.notify_error(f"SELL {symbol}", str(e))
                return

        # ─── Partial fill handling (C2) ───────────────────────────────────────
        # `remaining` is the qty Public still holds after the SELL.
        # - DRY_RUN path: skipped (no real order placed).
        # - Live path with `remaining is None`: portfolio check failed; fall
        #   back to the legacy "treat as full fill" behavior, which is at
        #   worst the same as before this fix.
        partial_fill = False
        if not self._dry_run and remaining is not None and remaining > 0:
            partial_fill = True
            sold_qty = max(0.0, size_estimate - remaining)
            if sold_qty <= 0:
                # Nothing actually sold — bail out without deleting the
                # position or recording an exit. Next cycle will retry.
                log_warn(
                    f"[trader] SELL {symbol} appears to have filled 0 (remaining={remaining:.8f}, "
                    f"requested={size_estimate:.8f}) — leaving position intact",
                    self.run_id,
                )
                discord.notify_error(
                    f"SELL {symbol}",
                    f"Order returned but no fill detected (still holding {remaining:.8f}).",
                )
                return
            log_warn(
                f"[trader] SELL {symbol} partial fill: sold {sold_qty:.8f}, "
                f"{remaining:.8f} still held — keeping position open with reduced size",
                self.run_id,
            )
            size_estimate = sold_qty  # PnL only on what actually filled

        # P&L from actual quantities, then subtract round-trip fees.
        # Public charges per-order (BUY + SELL = 2 × fee). DRY_RUN trades
        # incur no real fees so we leave gross P&L alone in that mode.
        # On a partial fill we still pay both fees: the BUY happened in full
        # at entry, and the SELL leg is one (incomplete) order.
        gross_proceeds = fill_price  * size_estimate
        gross_cost     = entry_price * size_estimate
        gross_pnl      = gross_proceeds - gross_cost

        if self._dry_run:
            fees = 0.0
            pnl  = gross_pnl
        else:
            fees = get_crypto_fee_per_order() * 2  # round trip
            pnl  = gross_pnl - fees

        mode = "DRY RUN " if self._dry_run else ""
        log(
            f"{mode}SELL {symbol} | fill={fill_price:.2f} | qty={size_estimate:.8f} | "
            f"gross={gross_pnl:+.4f} fees=${fees:.2f} net={pnl:+.4f} | reason={reason}"
            f"{' (PARTIAL)' if partial_fill else ''}",
            self.run_id,
        )

        full_reason = f"DRY_RUN/{reason}" if self._dry_run else reason
        if partial_fill:
            full_reason = f"{full_reason}/partial"
        supabase_logger.log_trade(symbol, "SELL", fill_price, size_estimate, pnl, full_reason, self.run_id, order_id=order_id)

        # Capital reflects only realised PnL (regardless of partial vs full).
        self.state["capital"] = self.state.get("capital", 0.0) + pnl

        if partial_fill:
            # Keep the position with the residual size so the next cycle can
            # close it. Don't record a win/loss yet (the trade isn't over),
            # don't record an exit (cooldown shouldn't start), don't send a
            # buy/sell notification — log + Supabase row are enough audit
            # until the position is fully unwound.
            self.positions[symbol]["size_estimate"] = remaining
            self.positions[symbol]["usd_amount"]    = remaining * entry_price
            return

        # Full fill path — original behavior.
        if pnl >= 0:
            record_win(self.state, symbol)
        else:
            record_loss(self.state, symbol)

        # Mark cooldown — block re-entry for COOLDOWN_CANDLES
        record_exit(self.state, symbol)

        if not self._dry_run:
            discord.notify_sell(symbol, fill_price, pnl, reason)

        del self.positions[symbol]

    def check_exit(self, symbol: str, price: float) -> str | None:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if price <= pos.get("stop_loss", float("-inf")):
            return "STOP_LOSS"
        if price >= pos.get("take_profit", float("inf")):
            return "TAKE_PROFIT"
        return None