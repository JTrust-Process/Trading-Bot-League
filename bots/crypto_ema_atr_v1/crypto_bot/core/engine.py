# crypto_bot/core/engine.py

import time
from crypto_bot.config.settings import (
    get_symbols, EMA_SLOW, is_dry_run,
    get_circuit_breaker_losses,
    get_min_signal_gap_pct,
    get_min_signal_profit_pct,
    get_min_hold_candles,
    get_cooldown_candles,
)
from crypto_bot.data.market_data import get_price
from crypto_bot.strategy.signal import generate_signal, regime_check, warmup_progress
from crypto_bot.execution.trader import Trader, compute_stop_levels, stop_pct_from_levels
from crypto_bot.risk.manager import RiskManager
from crypto_bot.state.state import (
    load_state, save_state,
    append_price, get_price_history,
    is_circuit_broken, get_consecutive_losses,
    should_notify_circuit_broken, mark_circuit_notified,
    candles_since_exit,
    mark_position_desync, is_position_desynced, clear_position_desync,
    should_notify_atr_fallback, mark_atr_fallback_notified,
)
from crypto_bot.exchange.public_api import (
    get_primary_account_id, get_crypto_position_quantity,
)
from crypto_bot.logging.logger import log, log_warn, log_error
from crypto_bot.logging.monitor import Monitor
from crypto_bot.notifications import discord


def _get_price_with_retry(symbol: str, attempts: int = 3, delay: float = 2.0) -> float | None:
    # Audit M7: callers always pass at least 2 attempts now (see below) so a
    # single transient failure no longer drops a candle entirely.
    attempts = max(attempts, 2)
    for i in range(attempts):
        try:
            return get_price(symbol)
        except Exception as e:
            if i < attempts - 1:
                time.sleep(delay)
            else:
                log_error(f"[engine] get_price({symbol}) failed after {attempts} attempts: {e}")
    return None


def _reconcile_with_public(state: dict, symbols: list, run_id: str | None) -> bool:
    """
    At startup, check Public's actual crypto holdings against local state.

    If Public holds crypto for a symbol but local state has no position
    (state.json reset, corruption, etc.), flag the symbol as desynced
    and block all future BUYs for it until manually resolved.

    If state has a position AND Public confirms it, clear any prior desync flag.

    We never auto-restore positions because we don't know the real entry price
    from Public's portfolio endpoint — better to block than guess wrong.

    Audit M6: now returns True on success / False on partial-or-total failure.
    Caller uses the flag to suppress BUYs for the cycle when we couldn't
    confirm the on-exchange state.
    """
    try:
        account_id = get_primary_account_id()
    except Exception as e:
        log_error(f"[engine] reconcile failed — could not get account_id: {e}", run_id)
        return False

    all_ok = True
    for symbol in symbols:
        try:
            real_qty = get_crypto_position_quantity(account_id, symbol)
            local_has_position = symbol in state.get("positions", {})
            real_has_position  = real_qty is not None and real_qty > 0

            if real_has_position and real_qty is not None and not local_has_position:
                # The dangerous case: Public has crypto we forgot about
                if not is_position_desynced(state, symbol):
                    mark_position_desync(state, symbol, real_qty)
                    log_error(
                        f"[engine] {symbol} DESYNC — Public holds {real_qty:.8f} but local state has no position. "
                        f"BUYs blocked until resolved. Manually close on Public or restore state.",
                        run_id,
                    )
                    if not is_dry_run():
                        discord.notify_error(
                            f"{symbol} POSITION DESYNC",
                            f"Public has {real_qty:.8f} {symbol} but bot state has no position. "
                            f"BUYs are now blocked for {symbol}. Manually close the position on Public, "
                            f"then the desync will clear on next run.",
                        )
                else:
                    qty_str = f"{real_qty:.8f}" if real_qty is not None else "unknown"
                    log_warn(f"[engine] {symbol} still desynced (real_qty={qty_str})", run_id)

            elif not real_has_position and is_position_desynced(state, symbol):
                # Desync was resolved (manual close on Public)
                clear_position_desync(state, symbol)
                log(f"[engine] {symbol} desync cleared — Public no longer holds {symbol}", run_id)

        except Exception as e:
            log_warn(f"[engine] reconcile {symbol} failed: {e}", run_id)
            all_ok = False

    return all_ok


def run(monitor: Monitor, run_id: str | None) -> None:
    symbols   = get_symbols()
    dry_run   = is_dry_run()
    cb_thresh = get_circuit_breaker_losses()

    min_gap_pct    = get_min_signal_gap_pct()
    min_profit_pct = get_min_signal_profit_pct()
    min_hold       = get_min_hold_candles()
    cooldown       = get_cooldown_candles()

    log(f"Run started — symbols={symbols} dry_run={dry_run}", run_id)

    state = load_state()
    log(
        f"State loaded — capital={state['capital']:.2f} "
        f"open_positions={list(state['positions'].keys())}",
        run_id,
    )

    # Reconcile against Public BEFORE any trade decisions.
    # Catches the worst case: state.json reset while Public still holds positions.
    # Audit M6: when reconcile fails (Public API hiccup, etc.) we suppress all
    # BUYs for the cycle. Exits still fire — those should always run, even on
    # stale state, because not selling a position the user wanted closed is
    # strictly worse than skipping a new entry.
    reconcile_ok = True
    if not dry_run:
        reconcile_ok = _reconcile_with_public(state, symbols, run_id)
        if not reconcile_ok:
            log_warn("[engine] Reconcile incomplete — BUYs suppressed for this cycle", run_id)

    trader = Trader(state, run_id)
    risk   = RiskManager(state)

    if not dry_run and discord.should_send_daily_summary(state):
        discord.notify_daily_summary(
            state=state,
            symbols=symbols,
            price_history=state.get("price_history", {}),
            dry_run=dry_run,
        )
        # Audit M5: persist immediately after sending so a crash later in
        # the cycle doesn't cause us to re-send the same summary on the
        # next run.
        save_state(state)
        log("Daily Discord summary sent", run_id)

    # ── Process each symbol ───────────────────────────────────────────────────
    for symbol in symbols:
        try:
            in_position = symbol in trader.positions

            if is_circuit_broken(state, symbol, cb_thresh):
                consecutive = get_consecutive_losses(state, symbol)
                log_warn(
                    f"{symbol} | CIRCUIT BREAKER — {consecutive} consecutive losses, skipping",
                    run_id,
                )
                monitor.log_event(
                    run_id, "circuit_breaker",
                    f"{symbol} paused after {consecutive} losses",
                )
                if not dry_run and should_notify_circuit_broken(state, symbol):
                    discord.notify_circuit_breaker(symbol, consecutive)
                    mark_circuit_notified(state, symbol)
                continue

            price = _get_price_with_retry(symbol, attempts=3 if in_position else 1)
            if price is None:
                if in_position:
                    log_error(
                        f"{symbol} | price fetch FAILED with open position — "
                        f"SL/TP will not fire this cycle",
                        run_id,
                    )
                else:
                    log_warn(f"{symbol} | price fetch failed, skipping cycle", run_id)
                continue

            append_price(state, symbol, price)
            prices          = get_price_history(state, symbol)
            signal, gap_pct = generate_signal(prices, in_position)

            if len(prices) < EMA_SLOW + 2:
                log(
                    f"{symbol} | price={price:.2f} | signal={signal} | warmup {warmup_progress(prices)}",
                    run_id,
                )
            else:
                log(
                    f"{symbol} | price={price:.2f} | signal={signal} | gap={gap_pct*100:.3f}% | "
                    f"history={len(prices)}",
                    run_id,
                )

            monitor.log_event(
                run_id, "signal",
                f"{symbol} signal={signal} price={price:.2f} gap_pct={gap_pct:.4f}",
            )

            # 1. Hard exits — ALWAYS fire
            exit_reason = trader.check_exit(symbol, price)
            if exit_reason:
                log(f"{symbol} | exit triggered: {exit_reason}", run_id)
                monitor.log_event(run_id, "exit", f"{symbol} {exit_reason} at {price:.2f}")
                trader.sell(symbol, price, exit_reason)
                continue

            # 2. Strategy BUY — gated by reconcile + desync + cooldown + regime + gap quality
            if signal == "BUY" and not in_position:

                # 2.0. Reconcile gate (M6) — if we couldn't confirm Public's state at
                # startup, skip BUYs this cycle. Exits still fired above.
                if not reconcile_ok:
                    log_warn(
                        f"{symbol} | BUY signal suppressed — reconcile incomplete this cycle",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} reconcile-fail block",
                    )
                    continue

                # 2a. Desync gate — refuse to buy if Public has crypto we don't track
                if is_position_desynced(state, symbol):
                    log_warn(
                        f"{symbol} | BUY signal suppressed — POSITION DESYNC (Public has untracked crypto)",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} desync block",
                    )
                    continue

                # 2b. Cooldown check — block re-entry for N candles after exit
                since_exit = candles_since_exit(state, symbol)
                if since_exit is not None and since_exit < cooldown:
                    log(
                        f"{symbol} | BUY signal suppressed — cooldown "
                        f"({since_exit}/{cooldown} candles since last exit)",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} cooldown active since_exit={since_exit}",
                    )
                    continue

                # 2c. Regime check — only trade in confirmed uptrends (or warmup blocks)
                is_uptrend, reason = regime_check(prices)
                if not is_uptrend:
                    log(
                        f"{symbol} | BUY signal suppressed — bad regime: {reason}",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} regime block: {reason}",
                    )
                    continue

                # 2d. Gap quality — filter weak crossovers
                if gap_pct < min_gap_pct:
                    log(
                        f"{symbol} | BUY signal suppressed — gap {gap_pct*100:.3f}% < "
                        f"min {min_gap_pct*100:.3f}%",
                        run_id,
                    )
                    monitor.log_event(run_id, "filtered", f"{symbol} weak BUY gap={gap_pct:.4f}")
                    continue

                # All filters passed — execute.
                #
                # Audit C3: compute the actual stop *before* sizing so the
                # risk-per-trade math uses the real stop distance instead of
                # a hardcoded 3%. We pass the computed sl_tp through to the
                # trader so it doesn't recompute (and possibly diverge from
                # what we just sized for).
                sl_lvl, tp_lvl, method = compute_stop_levels(symbol, price, run_id)
                stop_pct = stop_pct_from_levels(price, sl_lvl)

                # M3: alert (rate-limited per UTC day) when ATR fetch failed
                # and we're falling back to the fixed % stop. Otherwise the
                # bot silently behaves like the old strategy without anyone
                # noticing CoinGecko is broken.
                if method == "FIXED" and should_notify_atr_fallback(state, symbol):
                    mark_atr_fallback_notified(state, symbol)
                    if not dry_run:
                        discord.notify_error(
                            f"{symbol} ATR fallback",
                            "ATR computation failed — using fixed % SL/TP for new entries. "
                            "Check CoinGecko availability / rate limits.",
                        )

                log(
                    f"{symbol} | BUY filters passed — regime: {reason} | "
                    f"sizing with stop_pct={stop_pct*100:.2f}% ({method})",
                    run_id,
                )
                usd_amount = risk.position_size_usd(price, stop_pct=stop_pct)
                monitor.log_event(run_id, "buy", f"{symbol} ${usd_amount:.2f} at {price:.2f}")
                trader.buy(symbol, price, usd_amount, sl_tp=(sl_lvl, tp_lvl, method))

            # 3. Strategy SELL
            elif signal == "SELL" and in_position:
                pos              = trader.positions[symbol]
                entry_price      = pos["entry"]
                candles_at_entry = pos.get("candles_at_entry", pos.get("entry_candle", 0))
                candles_held     = len(prices) - candles_at_entry
                profit_pct       = (price / entry_price) - 1 if entry_price else 0.0

                if candles_held < min_hold:
                    log(
                        f"{symbol} | SELL signal suppressed — held only {candles_held} "
                        f"candles, need {min_hold}",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} too early to exit held={candles_held}",
                    )
                    continue

                if profit_pct < min_profit_pct:
                    log(
                        f"{symbol} | SELL signal suppressed — profit {profit_pct*100:.3f}% < "
                        f"min {min_profit_pct*100:.3f}% (fees would eat gain)",
                        run_id,
                    )
                    monitor.log_event(
                        run_id, "filtered",
                        f"{symbol} unprofitable signal pnl_pct={profit_pct:.4f}",
                    )
                    continue

                monitor.log_event(run_id, "sell", f"{symbol} signal exit at {price:.2f}")
                trader.sell(symbol, price, "signal")

        except Exception as e:
            msg = f"Error processing {symbol}: {e}"
            log_error(msg, run_id)
            monitor.log_error(run_id, f"engine/{symbol}", str(e))
            if not dry_run:
                discord.notify_error(f"engine/{symbol}", str(e))

    save_state(state)
    log(
        f"State saved — capital={state['capital']:.2f} "
        f"open_positions={list(state['positions'].keys())}",
        run_id,
    )