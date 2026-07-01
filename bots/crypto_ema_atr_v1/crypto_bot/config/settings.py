# crypto_bot/config/settings.py
#
# NO os.getenv() calls at module level.
# All values read lazily after load_dotenv() runs in __main__.

import os

# ── Hardcoded non-secrets ─────────────────────────────────────────────────────
SYMBOLS             = ["BTC", "ETH"]
INITIAL_CAPITAL     = 50.0
RISK_PER_TRADE      = 0.02
STOP_LOSS_PCT       = 0.03   # only used as fallback when ATR unavailable
TAKE_PROFIT_PCT     = 0.05   # only used as fallback when ATR unavailable
PRICE_HISTORY_SIZE  = 80     # bumped from 50 — need 50 for regime filter + buffer
EMA_FAST            = 9
EMA_SLOW            = 21
STATE_FILE          = "state.json"

# ── Risk guardrails ───────────────────────────────────────────────────────────
MAX_ORDER_AMOUNT_USD    = 25.0
CIRCUIT_BREAKER_LOSSES  = 3
MIN_BUYING_POWER_BUFFER = 25.0

# ── Signal quality filters ────────────────────────────────────────────────────
MIN_SIGNAL_GAP_PCT     = 0.0015
MIN_SIGNAL_PROFIT_PCT  = 0.03
MIN_HOLD_CANDLES       = 4

# ── Strategy 2 additions ──────────────────────────────────────────────────────
ATR_PERIOD             = 14    # ATR averaging window
ATR_SL_MULTIPLIER      = 2.0   # SL = entry - (2 × ATR)
ATR_TP_MULTIPLIER      = 4.0   # TP = entry + (4 × ATR)  → 2:1 reward:risk
COOLDOWN_CANDLES       = 4     # candles to wait after exit before re-entering same symbol

# ── Fee accounting ────────────────────────────────────────────────────────────
# Public charges $0.06 per crypto order. Round trip = $0.12.
# We subtract this from realised P&L on every SELL so dashboard numbers
# reflect what actually hits the account, not gross before fees.
CRYPTO_FEE_PER_ORDER   = 0.06


# ── Lazy loaders ──────────────────────────────────────────────────────────────

def get_public_api_key() -> str:
    val = os.getenv("PUBLIC_SECRET")
    if not val:
        raise RuntimeError("PUBLIC_SECRET env var not set")
    return val

def get_supabase_url() -> str:
    val = os.getenv("SUPABASE_URL")
    if not val:
        raise RuntimeError("SUPABASE_URL env var not set")
    return val

def get_supabase_key() -> str:
    val = os.getenv("SUPABASE_KEY")
    if not val:
        raise RuntimeError("SUPABASE_KEY env var not set")
    return val

def get_discord_webhook_url() -> str | None:
    return os.getenv("DISCORD_WEBHOOK_URL")

def get_symbols() -> list:
    raw = os.getenv("SYMBOLS")
    return list(dict.fromkeys(s.strip() for s in raw.split(","))) if raw else SYMBOLS

def is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "0").strip() in ("1", "true", "yes")

def _float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default

def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default

def get_initial_capital()         -> float: return _float_env("INITIAL_CAPITAL",         INITIAL_CAPITAL)
def get_max_order_usd()           -> float: return _float_env("MAX_ORDER_AMOUNT_USD",    MAX_ORDER_AMOUNT_USD)
def get_circuit_breaker_losses()  -> int:   return _int_env  ("CIRCUIT_BREAKER_LOSSES",  CIRCUIT_BREAKER_LOSSES)
def get_price_history_size()      -> int:   return _int_env  ("PRICE_HISTORY_SIZE",      PRICE_HISTORY_SIZE)
def get_min_buying_power_buffer() -> float: return _float_env("MIN_BUYING_POWER_BUFFER", MIN_BUYING_POWER_BUFFER)
def get_min_signal_gap_pct()      -> float: return _float_env("MIN_SIGNAL_GAP_PCT",      MIN_SIGNAL_GAP_PCT)
def get_min_signal_profit_pct()   -> float: return _float_env("MIN_SIGNAL_PROFIT_PCT",   MIN_SIGNAL_PROFIT_PCT)
def get_min_hold_candles()        -> int:   return _int_env  ("MIN_HOLD_CANDLES",        MIN_HOLD_CANDLES)
def get_atr_period()              -> int:   return _int_env  ("ATR_PERIOD",              ATR_PERIOD)
def get_atr_sl_multiplier()       -> float: return _float_env("ATR_SL_MULTIPLIER",       ATR_SL_MULTIPLIER)
def get_atr_tp_multiplier()       -> float: return _float_env("ATR_TP_MULTIPLIER",       ATR_TP_MULTIPLIER)
def get_cooldown_candles()        -> int:   return _int_env  ("COOLDOWN_CANDLES",        COOLDOWN_CANDLES)
def get_crypto_fee_per_order()    -> float: return _float_env("CRYPTO_FEE_PER_ORDER",    CRYPTO_FEE_PER_ORDER)