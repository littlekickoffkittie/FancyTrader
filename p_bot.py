#!/usr/bin/env python3
"""
Phemex Automated Trading Bot
==============================
Runs the dual scanner on a schedule, picks the best setups, and auto-executes.

Strategy:
  - $10 margin per trade at 30x cross leverage ($300 notional)
  - Market order entry
  - Immediately place 0.5% trailing stop (closeOnTrigger)
  - Max 1 concurrent open position
  - Won't re-enter a symbol already in position
  - Supports both LONG and SHORT (defaults to SHORT)

Auth (from Phemex API docs):
  Headers:
    x-phemex-access-token  : API Key ID
    x-phemex-request-expiry: Unix epoch seconds (now + 60s)
    x-phemex-request-signature: HMacSha256(path + queryString + expiry + body)

Key USDT-M endpoints used:
  GET  /public/products                  — instrument lot sizes
  GET  /g-accounts/accountPositions      — balance & open positions
  PUT  /g-positions/leverage             — set leverage per symbol
  PUT  /g-orders/create                  — place order (preferred)
  GET  /g-orders/activeList              — check open/pending orders

.env keys required:
  PHEMEX_API_KEY     = your API key ID
  PHEMEX_API_SECRET  = your API secret
  PHEMEX_BASE_URL    = https://testnet-api.phemex.com   (testnet)
                       https://api.phemex.com            (mainnet)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from websocket import WebSocketApp
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from colorama import init, Fore, Style
from dotenv import load_dotenv
import phemex_common as pc

# Telegram Configuration — load exclusively from environment; never hardcode credentials
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# ── Scanner imports ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import phemex_long as scanner_long
    import phemex_short as scanner_short
except ImportError as e:
    print(Fore.RED + f"[ERROR] Could not import scanner modules: {e}")
    print("Make sure phemex_long.py and phemex_short.py are in the same directory.")
    sys.exit(1)

load_dotenv()
init(autoreset=True)

# ────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────
BASE_URL       = os.getenv("PHEMEX_BASE_URL", "https://testnet-api.phemex.com")
API_KEY        = os.getenv("PHEMEX_API_KEY", "")
API_SECRET     = os.getenv("PHEMEX_API_SECRET", "")
BOT_LOG_FILE   = Path(SCRIPT_DIR) / "bot_trades.json"
BLACKLIST_FILE = Path(SCRIPT_DIR) / "bot_blacklist.json"

# Entity API Configuration
ENTITY_API_KEY      = os.getenv("ENTITY_API_KEY", "")
ENTITY_API_BASE_URL = os.getenv("ENTITY_API_BASE_URL", "https://acoustic-trade-scan-now.base44.app")
ENTITY_APP_ID       = os.getenv("ENTITY_APP_ID", pc.ENTITY_APP_ID)
SESSION_ID          = f"sess-{int(time.time())}"
ENABLE_ENTITY       = bool(ENTITY_API_KEY and ENTITY_APP_ID)

def make_entity_request(entity_name: str, method: str = "POST", data: dict = None, entity_id: str = None) -> Optional[dict]:
    """
    Sends data to the Entity API for persistence.
    """
    if not ENABLE_ENTITY:
        return None
    
    url = f"{ENTITY_API_BASE_URL}/api/apps/{ENTITY_APP_ID}/entities/{entity_name}"
    if entity_id:
        url += f"/{entity_id}"
        
    headers = {
        "api_key": ENTITY_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, params=data, timeout=10)
        elif method.upper() == "PUT":
            resp = requests.put(url, headers=headers, json=data, timeout=10)
        elif method.upper() == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, json=data, timeout=10)
        
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug(f"Entity API {method} {entity_name} failed: {e}")
        return None

# Strategy parameters
MARGIN_USDT    = float(os.getenv("BOT_MARGIN_USDT", "50.0"))   # $ margin per trade
LEVERAGE       = int(os.getenv("BOT_LEVERAGE", "30"))          # leverage multiplier
TRAIL_PCT      = float(os.getenv("BOT_TRAIL_PCT", "0.02"))     # 2% trailing stop
TAKE_PROFIT_PCT = float(os.getenv("BOT_TAKE_PROFIT_PCT", "0.02")) # 2% take profit
SCAN_INTERVAL  = int(os.getenv("BOT_SCAN_INTERVAL", "300"))    # seconds between scans
MIN_SCORE      = int(os.getenv("BOT_MIN_SCORE", "130"))        # minimum score to trade (consolidated)
MIN_SCORE_GAP  = int(os.getenv("BOT_MIN_SCORE_GAP", "30"))     # minimum gap between long/short scores
DIRECTION      = os.getenv("BOT_DIRECTION", "SHORT")           # default to SHORT to match sim_bot.py
TIMEFRAME      = os.getenv("BOT_TIMEFRAME", "4H")              # match sim_bot.py default
MIN_VOLUME     = int(os.getenv("BOT_MIN_VOLUME", "1000000"))
MAX_WORKERS    = int(os.getenv("BOT_MAX_WORKERS", "30"))
MAX_POSITIONS  = int(os.getenv("BOT_MAX_POSITIONS", "3")) # default to 3 positions
RATE_LIMIT_RPS = float(os.getenv("BOT_RATE_LIMIT_RPS", "20.0"))

# Position Mode: OneWay (posSide="Merged") or Hedged (posSide="Long"/"Short")
POSITION_MODE = os.getenv("BOT_POSITION_MODE", "OneWay")  # "OneWay" or "Hedged"

# ── Simulation-like features for production ─────────────────────────
_live_prices: Dict[str, float] = {}
_prices_lock = threading.Lock()
_ws_app = None
_ws_thread = None
_slot_available_event = threading.Event()
_display_paused = threading.Event()
_display_thread_running = False
_ws_connected = False # New flag to track WebSocket connection status

_fast_track_opened: set[str] = set()
_fast_track_lock = threading.RLock()

# Local state for dashboard stop display and trade tracking
_local_stop_states: Dict[str, dict] = {} # symbol -> {stop_price, high_water, low_water, entry_time, entry_score, direction}

FAST_TRACK_SCORE = int(os.getenv("BOT_FAST_TRACK_SCORE", str(pc.SCORE_FAST_TRACK)))
FAST_TRACK_COOLDOWN: Dict[str, float] = {}  # symbol → timestamp of last fast-track
FAST_TRACK_COOLDOWN_SECONDS = 300  # 5 minutes before same symbol can fast-track again
RESULT_STALENESS_SECONDS = 120  # discard scan results older than 2 minutes

# ── Symbol Blacklist / Cooldown ────────────────────────
_stop_contexts: Dict[str, pc.StopContext] = {} # symbol -> StopContext
_blacklist_lock = threading.Lock()

def get_tf_seconds(tf: str) -> int:
    """Helper to convert timeframe string to seconds."""
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200, "1D": 86400
    }
    return mapping.get(tf, 900) # default 15m

def save_blacklist() -> None:
    """Saves the current _stop_contexts to a file."""
    with _blacklist_lock:
        data_to_save = {s: ctx.__dict__ for s, ctx in _stop_contexts.items() if time.time() - ctx.timestamp < 48 * 3600}
        try:
            BLACKLIST_FILE.write_text(json.dumps(data_to_save))
        except Exception as e:
            logger.error(f"Failed to save cooldowns: {e}")

def load_blacklist() -> None:
    """Loads the cooldown contexts from a file and cleans up expired entries."""
    global _stop_contexts
    if BLACKLIST_FILE.exists():
        try:
            loaded_data = json.loads(BLACKLIST_FILE.read_text())
            with _blacklist_lock:
                _stop_contexts = {}
                for s, val in loaded_data.items():
                    if isinstance(val, dict):
                        if time.time() - val["timestamp"] < 48 * 3600:
                            _stop_contexts[s] = pc.StopContext(**val)
                    else:
                        # Legacy format support
                        _stop_contexts[s] = pc.StopContext(timestamp=float(val), price=0.0, atr=0.0, spread=0.05)
            logger.info(f"Loaded {len(_stop_contexts)} active cooldown entries.")
        except Exception as e:
            logger.error(f"Failed to load cooldowns: {e}")

def capture_stop_context(symbol: str, price: float, is_stop_loss: bool = True, direction: str = "SHORT") -> pc.StopContext:
    """Captures market state at the moment of exit for dynamic cooldown tracking."""
    # 1. ATR
    try:
        candles = pc.get_candles(symbol, timeframe=TIMEFRAME, limit=100)
        if candles:
            closes = [float(c[6]) for c in candles]
            highs  = [float(c[4]) for c in candles]
            lows   = [float(c[5]) for c in candles]
            atr = pc.calc_atr(highs, lows, closes) or 0.0
        else:
            atr = 0.0
    except Exception:
        atr = 0.0
    
    # 2. Spread
    try:
        _, _, spread, _, _ = pc.get_order_book(symbol)
        if spread is None: spread = 0.05
    except Exception:
        spread = 0.05
        
    # 3. Handle Stop Count (Consecutive losses)
    new_count = 1
    with _blacklist_lock:
        if symbol in _stop_contexts:
            old_ctx = _stop_contexts[symbol]
            if is_stop_loss:
                # Increment if it's another stop loss
                new_count = getattr(old_ctx, "stop_count", 1) + 1
            else:
                # Reset if it's a Take Profit
                new_count = 0
        elif not is_stop_loss:
            # TP on a symbol not currently in cooldown
            new_count = 0

    return pc.StopContext(
        timestamp=time.time(),
        price=price,
        atr=atr,
        spread=spread / 100.0 if spread else 0.0005, # convert % to decimal
        direction=direction,
        stop_count=new_count
    )

def blacklist_symbol(symbol: str, price: float = 0.0, reason: str = "stop_out", is_stop_loss: bool = True, direction: str = "SHORT") -> None:
    """Register a cooldown for a symbol, capturing its current context."""
    ctx = capture_stop_context(symbol, price, is_stop_loss=is_stop_loss, direction=direction)
    with _blacklist_lock:
        _stop_contexts[symbol] = ctx
    msg = f"🚫 *COOLDOWN REGISTERED* — {symbol} context captured (Count: {ctx.stop_count}) after {reason}"
    logger.info(msg)
    send_telegram_message(msg)
    save_blacklist()

def get_readiness_info(symbol: str, result: dict, direction: str = "SHORT") -> Tuple[float, float]:
    """
    Calculates Readiness Score R and position size scalar for a symbol under cooldown.
    Returns (R, scalar).
    """
    with _blacklist_lock:
        stop_ctx = _stop_contexts.get(symbol)
        if not stop_ctx:
            return 1.0, 1.0
            
    # ATR from result
    last_price = result["price"]
    atr_stop_pct = result.get("atr_stop_pct", 0.0)
    current_atr = (atr_stop_pct * last_price / 50.0) if atr_stop_pct > 0 else 0.0
    
    # Spread from result
    current_spread_pct = result.get("spread", 0.05)
    current_spread = current_spread_pct / 100.0 * last_price
    
    # Order Book Volumes
    try:
        _, _, _, bid_vol, ask_vol = pc.get_order_book(symbol)
    except Exception:
        bid_vol, ask_vol = 1.0, 1.0
        
    # Candles
    candles_raw = pc.get_candles(symbol, timeframe=TIMEFRAME, limit=24)
    candles = [(float(c[3]), float(c[4]), float(c[5]), float(c[6])) for c in candles_raw]
    
    r = pc.calc_readiness_score(
        symbol=symbol,
        stop_ctx=stop_ctx,
        current_atr=current_atr,
        current_spread=current_spread,
        bid_vol=bid_vol,
        ask_vol=ask_vol,
        candles=candles,
        buy_vol=None,
        sell_vol=None,
        intended_direction=direction
    )
    
    if r >= 0.95:
        with _blacklist_lock:
            if symbol in _stop_contexts:
                del _stop_contexts[symbol]
        save_blacklist()
        tui_log(f"{symbol}: Cooldown cleared (R={r:.2f})")
        return r, 1.0
        
    return r, pc.get_readiness_scalar(r)

def is_blacklisted(symbol: str) -> bool:
    """Legacy check - still returns True if R < 0.35, but most logic should use get_readiness_info."""
    # This is a bit tricky because is_blacklisted doesn't have the scanner 'result'.
    # We'll do a simple time-based check as fallback or just return False and let execute_setup handle it.
    with _blacklist_lock:
        return symbol in _stop_contexts

# ── Volatility / Liquidity Adjusted Parameters ────────
# Log analysis: Low-Liquidity trades → 45% of losses, only 22% of wins.
# For low-liq assets, widen the stop and reduce leverage to avoid the shakeout.
LOW_LIQ_LEVERAGE = int(os.getenv("BOT_LOW_LIQ_LEVERAGE", "10"))      # 10x for wide-spread assets
LOW_LIQ_TRAIL_PCT = float(os.getenv("BOT_LOW_LIQ_TRAIL", "0.012"))  # 1.2% trail for low-liq
LOW_LIQ_MARGIN = float(os.getenv("BOT_LOW_LIQ_MARGIN", "5.0"))      # $5 margin for low-liq (smaller bet)

# ── Minimum Viable Score ──────────────────────────────
# Log analysis: avg win score = 126.0, avg loss score = 126.7 — score alone is NOT predictive.
# Best total PnL occurs at score >= 130. HTF Alignment present in 33% of wins, 0% of losses.
# The new gating logic: base score >= 130 OR (score >= 120 AND HTF_aligned).
# Without HTF, Low-Liquidity assets must score >= 145 to offset the noise penalty.
MIN_SCORE_DEFAULT = int(os.getenv("BOT_MIN_SCORE", "130"))
MIN_SCORE_HTF_BYPASS = int(os.getenv("BOT_MIN_SCORE_HTF", "120")) # lower bar if HTF aligned
MIN_SCORE_LOW_LIQ = int(os.getenv("BOT_MIN_SCORE_LOW_LIQ", "145")) # higher bar for low-liq

# ── Dynamic Scaling ───────────────────────────────────
# At $26 fuel, run max 2 concurrent positions to protect capital.
# As equity grows, allow more positions: $50→3, $75→4, $100+→5
SCALING_TIERS: List[Tuple[float, int]] = [
    (100.0, 5),
    (75.0,  4),
    (50.0,  3),
    (30.0,  2),
    (0.0,   1),  # survival mode below $30
]

def get_dynamic_max_positions(balance: float) -> int:
    """Return the maximum concurrent positions allowed for the given equity level, capped by MAX_POSITIONS."""
    actual_max = 1
    for threshold, max_pos in SCALING_TIERS:
        if balance >= threshold:
            actual_max = max_pos
            break
    return min(actual_max, MAX_POSITIONS)

# Account-level trailing stop
_account_high_water: float = 0.0  # peak equity seen
ACCOUNT_TRAIL_PCT = float(os.getenv("BOT_ACCOUNT_TRAIL_PCT", "0.05")) # 5% trail on peak equity
_account_trail_stop: float = 0.0  # current account stop level
_account_trading_halted: bool = False

# Cache for dashboard
_cached_balance: float = 0.0
_cached_positions: List[dict] = []
_cache_lock = threading.Lock()

def send_telegram_message(message: str) -> None:
    """Sends a message to the configured Telegram chat."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

# ── Logging Setup ─────────────────────────────────────────────────────
from collections import deque
_bot_logs = deque(maxlen=100)

logger = pc.setup_colored_logging(
    "phemex_bot", 
    level=logging.INFO, 
    log_file=Path(SCRIPT_DIR) / "bot.log",
    buffer=_bot_logs
)

def tui_log(msg: str, event_type: str = "BOT") -> None:
    """Logs a message to both the system audit log and the TUI buffer."""
    pc.log_system_event(event_type, msg)
    # Ensure it also goes into our local logger which is hooked to the TUI deque
    logger.info(msg)

# ────────────────────────────────────────────────────────────────────
# HTTP session
# ────────────────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    sess = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    return sess

_session = build_session()

# ────────────────────────────────────────────────────────────────────
# Phemex HMAC auth
# ────────────────────────────────────────────────────────────────────

def _sign(path: str, query: str, expiry: int, body: str) -> str:
    """
    HMacSha256(URL Path + QueryString + Expiry + body)
    Exactly as documented: path + queryString (no '?') + expiry + body
    """
    message = path + query + str(expiry) + body
    # logger.info(f"Signing message: {message}") # Debug
    sig = hmac.new(
        key=API_SECRET.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256
    ).hexdigest()
    # logger.info(f"Generated signature: {sig}") # Debug
    return sig


def _auth_headers(path: str, query: str = "", body: str = "") -> dict:
    expiry = int(time.time()) + 60
    signature = _sign(path, query, expiry, body)
    headers = {
        "x-phemex-access-token": API_KEY,
        "x-phemex-request-expiry": str(expiry),
        "x-phemex-request-signature": signature,
        "Content-Type": "application/json",
    }
    # logger.info(f"Auth headers: {headers}") # Debug
    return headers


def _get(path: str, params: dict = None) -> Optional[dict]:
    params = params or {}
    query = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    url = BASE_URL + path + (("?" + query) if query else "")
    headers = _auth_headers(path, query)
    try:
        resp = _session.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        log_error_response(path, resp)
        return None
    except Exception as e:
        logger.error("GET %s failed: %s", path, e)
        return None


def _put(path: str, params: dict = None, body: dict = None) -> Optional[dict]:
    params = params or {}
    query = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    body_str = json.dumps(body) if body else ""
    url = BASE_URL + path + (("?" + query) if query else "")
    headers = _auth_headers(path, query, body_str)
    try:
        resp = _session.put(url, headers=headers, data=body_str, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        log_error_response(path, resp)
        return None
    except Exception as e:
        logger.error("PUT %s failed: %s", path, e)
        return None


def _post(path: str, body: dict = None) -> Optional[dict]:
    body = body or {}
    body_str = json.dumps(body)
    headers = _auth_headers(path, "", body_str)
    url = BASE_URL + path
    try:
        resp = _session.post(url, headers=headers, data=body_str, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        log_error_response(path, resp)
        return None
    except Exception as e:
        logger.error("POST %s failed: %s", path, e)
        return None


def _delete(path: str, params: dict = None) -> Optional[dict]:
    params = params or {}
    query = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    url = BASE_URL + path + (("?" + query) if query else "")
    headers = _auth_headers(path, query)
    try:
        resp = _session.delete(url, headers=headers, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        log_error_response(path, resp)
        return None
    except Exception as e:
        logger.error("DELETE %s failed: %s", path, e)
        return None

def log_error_response(path: str, resp: requests.Response):
    """Log detailed error information from Phemex API response."""
    status_code = resp.status_code
    error_msg = ""
    exchange_code = None
    
    try:
        data = resp.json()
        exchange_code = data.get("code")
        phemex_msg = data.get("msg")
        phemex_data_snippet = json.dumps(data.get("data", {}))[:200]
        error_msg = f"Phemex API error for {path}: HTTP {status_code}, Phemex Code {exchange_code}, Msg: '{phemex_msg}', Data: {phemex_data_snippet}"
        logger.error(error_msg)
    except json.JSONDecodeError:
        error_msg = f"Phemex API error for {path}: HTTP {status_code}, Raw response: {resp.text[:200]}"
        logger.error(error_msg)
    except Exception as e:
        error_msg = f"Phemex API error for {path}: HTTP {status_code}, Error parsing response: {e}"
        logger.error(error_msg)
        
    # Entity API Hook
    make_entity_request("errorevent", data={
        "error_id": f"err-{int(time.time()*1000)}",
        "timestamp": datetime.datetime.now().isoformat(),
        "session_id": SESSION_ID,
        "error_type": "API_ERROR",
        "severity": "CRITICAL" if status_code >= 500 else "WARNING",
        "http_status": status_code,
        "exchange_code": str(exchange_code) if exchange_code else None,
        "message": error_msg,
        "context": path
    })


# ────────────────────────────────────────────────────────────────────
# WebSocket & Live Monitoring
# ────────────────────────────────────────────────────────────────────

def _ws_on_message(ws, message):
    try:
        data = json.loads(message)
        # Handle both formats:
        # 1. Old format: {"market24h_p": {"symbol": "...", "closeRp": "..."}}
        # 2. New format: {"topic": "market24h_p", "data": [{"symbol": "...", "closeRp": "..."}]}
        ticks = []
        if "market24h_p" in data:
            ticks = [data["market24h_p"]]
        elif data.get("topic") == "market24h_p" and isinstance(data.get("data"), list):
            ticks = data["data"]
            
        for tick in ticks:
            symbol = tick.get("symbol")
            close_rp = tick.get("closeRp")
            if symbol and close_rp is not None:
                price = float(close_rp)
                with _prices_lock:
                    _live_prices[symbol] = price
                _check_stops_live(symbol)
                _check_account_trail()
    except Exception as e:
        logger.debug(f"WS Message error: {e}")


def _check_stops_live(symbol):
    """
    Update local trailing stop state based on price movement.
    Uses WebSocket live prices primarily, falls back to REST API if WS is down.
    """
    if symbol not in _local_stop_states:
        return
    
    current = None
    with _prices_lock:
        current = _live_prices.get(symbol)
            
    if current is None: # If price not available from WS
        if not _ws_connected: # And WS is explicitly disconnected
            logger.warning(f"WS disconnected. Attempting REST API fallback for {symbol} in _check_stops_live.")
            current = _get_current_price_rest(symbol)
            if current:
                with _prices_lock:
                    _live_prices[symbol] = current # Update _live_prices with REST price for consistency
                logger.info(f"Successfully obtained REST API price for {symbol} in _check_stops_live.")
            else:
                logger.warning(f"Failed to get price for {symbol} from REST API. Cannot check stop.")
                return # Cannot check stop without a price
        else: # WS is connected but price is missing from _live_prices (might be new symbol not subscribed yet)
            logger.debug(f"WS connected but no live price for {symbol}. Waiting for WS update.")
            return

    if not current: return # Should be covered by above, but as a safeguard

    state = _local_stop_states[symbol]
    direction = state["direction"]
    
    if direction == "LONG":
        # Trailing stop high-water mark
        if current > state.get("high_water", 0.0):
            state["high_water"] = current
            state["stop_price"] = current * (1.0 - TRAIL_PCT)
    else:
        # Trailing stop low-water mark
        if current < state.get("low_water", 999999999.0):
            state["low_water"] = current
            state["stop_price"] = current * (1.0 + TRAIL_PCT)


def _check_account_trail():
    global _account_high_water, _account_trail_stop, _account_trading_halted
    with _cache_lock:
        balance = _cached_balance
        positions = _cached_positions
    if balance == 0 and not positions: return
    
    total_upnl = 0.0
    with _prices_lock:
        for pos in positions:
            sym = pos["symbol"]
            now = _live_prices.get(sym)
            
            if now is None: # If price not available from WS
                if not _ws_connected: # And WS is explicitly disconnected
                    logger.warning(f"WS disconnected. Attempting REST API fallback for {sym} in _check_account_trail.")
                    now = _get_current_price_rest(sym)
                    if now:
                        _live_prices[sym] = now # Update _live_prices
                        logger.info(f"Successfully obtained REST API price for {sym} in _check_account_trail.")
                    else:
                        logger.warning(f"Failed to get price for {sym} from REST API. Skipping position check.")
                        continue # Skip this position if price is unavailable
                else: # WS is connected but price is missing from _live_prices
                    logger.debug(f"WS connected but no live price for {sym}. Waiting for WS update.")
                    continue # Skip this position for now, hoping WS delivers it soon
            
            if now is None: continue # If price still None after all attempts
            
            side = pos["side"]
            entry = pos["entry"]
            size = float(pos["size"]) # Buy = Long, Sell = Short
            upnl = (now - entry) * size if side == "Buy" else (entry - now) * size
            total_upnl += upnl
                
    equity = balance + total_upnl
    if _account_high_water == 0:
        _account_high_water = equity
        _account_trail_stop = equity * (1 - ACCOUNT_TRAIL_PCT)
        
    if equity > _account_high_water:
        _account_high_water = equity
        _account_trail_stop = equity * (1 - ACCOUNT_TRAIL_PCT)
        
    if not _account_trading_halted and equity < _account_trail_stop:
        _account_trading_halted = True
        msg = f"⛔ *ACCOUNT TRAIL STOP HIT* — Peak: ${ _account_high_water:.2f} Current: ${equity:.2f} Stop: ${_account_trail_stop:.2f} — trading halted"
        print(Fore.RED + Style.BRIGHT + f"\n {msg}")
        send_telegram_message(msg)
    elif _account_trading_halted and equity >= _account_trail_stop:
        _account_trading_halted = False
        msg = f"✅ *ACCOUNT RECOVERED* — Current: ${equity:.2f} (Stop: ${_account_trail_stop:.2f}) — resuming"
        print(Fore.GREEN + Style.BRIGHT + f"\n {msg}")
        send_telegram_message(msg)


def _ws_on_open(ws):
    global _ws_connected
    _ws_connected = True
    logger.info("WS Connection Opened")
    positions = get_open_positions()
    symbols = [p["symbol"] for p in positions]
    if symbols:
        sub = {"id": 1, "method": "market24h_p.subscribe", "params": symbols}
        ws.send(json.dumps(sub))

def _ws_on_error(ws, error):
    global _ws_connected
    _ws_connected = False
    logger.error(f"WS Error: {error}")

def _ws_on_close(ws, close_status_code, close_msg):
    global _ws_connected
    _ws_connected = False
    logger.warning(f"WS Connection Closed: Status Code={close_status_code}, Message={close_msg}")
    # For now, _ws_run_loop will handle reconnection after a short delay

def _ws_heartbeat(ws, stop_event):
    while not stop_event.is_set():
        time.sleep(15)
        try:
            if ws.sock and ws.sock.connected:
                ws.send(json.dumps({"id": 0, "method": "server.ping", "params": []}))
                logger.debug(f"WS Heartbeat sent. Connected: {_ws_connected}")
            else:
                logger.debug(f"WS Heartbeat skipped. Connected: {_ws_connected}")
        except Exception as e:
            logger.debug(f"WS Heartbeat error: {e}")
            break


def _ws_run_loop():
    global _ws_app
    ws_url = "wss://ws.phemex.com"
    if "testnet" in BASE_URL:
        ws_url = "wss://testnet.phemex.com/ws"
    
    reconnect_delay = 1  # Start with 1 second delay
    max_reconnect_delay = 60 # Max delay of 60 seconds
    
    while True:
        try:
            logger.info(f"Attempting WS connection... (current _ws_connected: {_ws_connected}, next retry in {reconnect_delay}s)")
            stop_event = threading.Event()
            _ws_app = WebSocketApp(
                ws_url,
                on_message=_ws_on_message,
                on_open=_ws_on_open,
                on_error=_ws_on_error,
                on_close=_ws_on_close
            )
            # Start heartbeat with stop signal
            threading.Thread(target=_ws_heartbeat, args=(_ws_app, stop_event), daemon=True).start()
            
            # This will block until connection closes or error
            _ws_app.run_forever()
            
            # Signal heartbeat to stop
            stop_event.set()
            
            # Use exponential backoff for reconnection
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            
            # If we were connected for more than 10 seconds, reset backoff
            # (Note: this is a simple heuristic, a better one would track connection duration)
            
        except Exception as e:
            logger.error(f"WS run loop error: {e}")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            logger.error(f"WS Run loop error: {e}. Reconnecting in {reconnect_delay}s...")
        
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay) # Exponential backoff


def _ensure_ws_started():
    global _ws_thread
    if _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=_ws_run_loop, daemon=True)
        _ws_thread.start()


def _cache_refresher():
    """Periodically refresh balance and positions for the dashboard/trail-stop."""
    global _cached_balance, _cached_positions
    while True:
        try:
            nb, np = get_account_status()
            if nb is not None:
                with _cache_lock:
                    # Detect closure for logging
                    old_symbols = {p["symbol"] for p in _cached_positions}
                    new_symbols = {p["symbol"] for p in np}
                    
                    closed = old_symbols - new_symbols
                    if closed:
                        # Detect closure for logging
                        for sym in closed:
                            old_p = next((p for p in _cached_positions if p["symbol"] == sym), None)
                            local_state = _local_stop_states.pop(sym, {})
                            
                            # Attempt to fetch the actual realized PnL from the exchange
                            realized_pnl = get_recent_realized_pnl(sym)
                            
                            # Recover data from history if missing in cache
                            history = []
                            if BOT_LOG_FILE.exists():
                                try: history = json.loads(BOT_LOG_FILE.read_text())
                                except Exception as e:
                                    logger.warning(f"Failed to load history for symbol {sym}: {e}")
                            h_entry = next((h for h in reversed(history) if h.get("symbol") == sym and h.get("status") == "entered"), None)
                            
                            entry_price = old_p.get("entry", 0) if old_p else (h_entry.get("price", 0) if h_entry else 0)
                            qty_str = str(old_p.get("size", 0)) if old_p else (str(h_entry.get("qty", 0)) if h_entry else "0")
                            score = local_state.get("entry_score", 0) or (h_entry.get("score", 0) if h_entry else 0)
                            entry_time = local_state.get("entry_time") or (datetime.datetime.fromisoformat(h_entry["timestamp"]) if h_entry else datetime.datetime.now())
                            direction = local_state.get("direction", "LONG" if (old_p and old_p["side"]=="Buy") else (h_entry.get("direction") if h_entry else "Unknown"))
                            
                            hold_secs = (datetime.datetime.now() - entry_time).total_seconds() if entry_time else 0
                            h_min, h_sec = divmod(int(hold_secs), 60)
                            h_hour, h_min = divmod(h_min, 60)
                            dur_str = f"{h_hour}h {h_min}m" if h_hour > 0 else (f"{h_min}m {h_sec}s" if h_min > 0 else f"{h_sec}s")
                            
                            symbol_to_log = sym
                            side_to_log = old_p['side'] if old_p else ("Buy" if direction == "LONG" else "Sell")
                            
                            msg = f"🔔 *TRADE CLOSED (Exchange Stop)* — {symbol_to_log} {side_to_log} | PnL: {realized_pnl:+.4f} | Duration: {dur_str}"
                            print(Fore.RED + f"\n {msg}")
                            send_telegram_message(msg)
                            logger.info(msg)
                            
                            log_trade({
                                "timestamp": datetime.datetime.now().isoformat(),
                                "symbol": sym,
                                "direction": direction,
                                "price": entry_price,
                                "qty": qty_str,
                                "score": score,
                                "status": "closed",
                                "reason": "exchange_stop",
                                "pnl": round(float(realized_pnl), 4),
                                "hold_time_seconds": int(hold_secs),
                            })
                            
                            # ── Auto-Blacklist on Closure ──────────
                            # Derive exit price from PnL if possible, else use entry
                            try:
                                size_f = float(qty_str)
                                if size_f > 0:
                                    # pnl = (exit - entry) * size -> exit = entry + pnl/size (for Long)
                                    # pnl = (entry - exit) * size -> exit = entry - pnl/size (for Short)
                                    if direction == "LONG":
                                        exit_price_derived = entry_price + (float(realized_pnl) / size_f)
                                    else:
                                        exit_price_derived = entry_price - (float(realized_pnl) / size_f)
                                else:
                                    exit_price_derived = entry_price
                            except Exception:
                                exit_price_derived = entry_price
                                
                            blacklist_symbol(
                                sym, price=exit_price_derived, 
                                reason=f"trade closure (PnL: ${realized_pnl:.2f})",
                                is_stop_loss=(realized_pnl <= 0),
                                direction=direction
                            )

                        _slot_available_event.set()
                    
                    # Entity API: Account Snapshot
                total_upnl = sum(p.get("pnl", 0.0) for p in np)
                equity = nb + total_upnl
                drawdown = 0.0
                if _account_high_water > 0:
                    drawdown = (_account_high_water - equity) / _account_high_water * 100
                
                make_entity_request("accountsnapshot", data={
                    "snapshot_id": f"acc-{int(time.time())}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "trigger": "CACHE_REFRESH",
                    "balance_usdt": nb,
                    "unrealised_pnl": total_upnl,
                    "equity": equity,
                    "peak_equity": _account_high_water,
                    "account_trail_stop": _account_trail_stop,
                    "drawdown_from_peak_pct": drawdown,
                    "trading_halted": _account_trading_halted,
                    "open_positions": len(np),
                    "max_positions_allowed": get_dynamic_max_positions(nb)
                })

                # Entity API: Positions
                for pos in np:
                    make_entity_request("position", data={
                        "position_id": f"pos-{pos['symbol']}-{int(time.time())}",
                        "symbol": pos["symbol"],
                        "side": pos["side"],
                        "size": pos["size"],
                        "entry_price": pos["entry"],
                        "unrealised_pnl": pos.get("pnl", 0.0),
                        "leverage": LEVERAGE, # best guess
                        "last_updated": datetime.datetime.now().isoformat()
                    })

                _cached_balance = nb
                _cached_positions = np
        except Exception as e:
            logger.error(f"Cache refresh error: {e}")
        time.sleep(30)


def _subscribe_symbol(symbol):
    def _do_sub():
        time.sleep(2)
        if _ws_app and _ws_app.sock and _ws_app.sock.connected:
            sub = {"id": 1, "method": "market24h_p.subscribe", "params": [symbol]}
            _ws_app.send(json.dumps(sub))
    threading.Thread(target=_do_sub, daemon=True).start()


def _live_pnl_display():
    global _display_thread_running
    while True:
        if _display_paused.is_set():
            time.sleep(1)
            continue
            
        # We read from cache here, which is updated in the bot_loop
        with _cache_lock:
            balance = _cached_balance
            positions = _cached_positions
            
        # Load history for stats
        history = []
        if BOT_LOG_FILE.exists():
            try: 
                all_trades = json.loads(BOT_LOG_FILE.read_text())
                # Filter for closed trades (have realized pnl)
                history = [t for t in all_trades if t.get("status") == "closed" or "pnl" in t and t["pnl"] != 0]
            except Exception as e:
                logger.warning(f"Failed to load trades for dashboard: {e}")

        # Clear screen
        sys.stdout.write("\033[2J\033[H")
        print(Fore.CYAN + pc.BANNER)
        print(Fore.CYAN + Style.BRIGHT + f" 📊 LIVE PRODUCTION DASHBOARD | {datetime.datetime.now().strftime('%H:%M:%S')}")
        print(Fore.CYAN + "─" * 70)
        
        total_upnl = 0.0
        if not positions:
            print(Fore.WHITE + " (No active positions)")
        else:
            for pos in positions:
                sym = pos["symbol"]
                with _prices_lock:
                    now = _live_prices.get(sym)
                    if now is None:
                        print(f" {pos['side']:4} {sym:<12} | Entry: {pos['entry']:.6g} | Waiting for price...")
                        continue
                    
                    side = pos["side"]
                    entry = pos["entry"]
                    size = float(pos["size"])
                    upnl = (now - entry) * size if side == "Buy" else (entry - now) * size
                    total_upnl += upnl
                    
                    # Stop Distance calculation
                    stop_dist_str = ""
                    local_state = _local_stop_states.get(sym)
                    if local_state and "stop_price" in local_state:
                        stop_px = local_state["stop_price"]
                        if side == "Buy":
                            stop_dist = (now - stop_px) / now * 100
                        else:
                            stop_dist = (stop_px - now) / now * 100
                        stop_dist_str = f" | Stop dist: {stop_dist:.2f}%"
                    
                    # Duration calculation
                    dur_str = ""
                    if sym in _local_stop_states:
                        ls = _local_stop_states[sym]
                        et = ls.get("entry_time")
                        if et:
                            diff = datetime.datetime.now() - et
                            tot_sec = int(diff.total_seconds())
                            if tot_sec < 60: dur_str = f"({tot_sec}s)"
                            elif tot_sec < 3600: dur_str = f"({tot_sec//60}m)"
                            else: dur_str = f"({tot_sec//3600}h {(tot_sec%3600)//60}m)"
                    dur_badge = Fore.WHITE + f"{dur_str}" + Style.RESET_ALL

                    dir_sym = "▲" if side == "Buy" else "▼"
                    dir_color = Fore.GREEN if side == "Buy" else Fore.RED
                    pnl_color = Fore.GREEN if upnl >= 0 else Fore.RED
                    leverage_str = f"| {pos.get('leverage', '??')}x" if pos.get('leverage') else ""
                    print(f" {dir_color}{dir_sym}{Style.RESET_ALL} {sym:<12} | Entry: {entry:.6g} | Now: {now:.6g} | "
                          f"uPnL: {pnl_color}{upnl:+.4f} USDT{Style.RESET_ALL}{leverage_str}{stop_dist_str} | {dur_badge}")
        
        equity = balance + total_upnl
        print(Fore.CYAN + "─" * 70)
        halt_status = f" | {Fore.RED}HALTED{Style.RESET_ALL}" if _account_trading_halted else ""
        print(f" Wallet: {balance:.2f} USDT | uPnL: {total_upnl:+.4f} USDT | {Style.BRIGHT}Equity: {equity:.2f} USDT{halt_status}")
        
        dynamic_max_pos = get_dynamic_max_positions(balance)
        print(f" Account Peak: ${ _account_high_water:.2f} | Account Stop: ${_account_trail_stop:.2f} | Max Positions: {dynamic_max_pos}")

        # Show active blacklist
        with _blacklist_lock:
            bl_active = {sym: expiry for sym, expiry in SYMBOL_BLACKLIST.items() if time.time() < expiry}
            if bl_active:
                bl_strs = ", ".join(f"{sym}({(exp-time.time())/60:.0f}m)" for sym, exp in bl_active.items())
                print(Fore.YELLOW + f" 🚫 COOLDOWN: {bl_strs}")
        
        # Stats Line
        if history:
            wins = [t for t in history if t.get("pnl", 0) > 0]
            losses = [t for t in history if t.get("pnl", 0) <= 0]
            win_rate = (len(wins) / len(history) * 100) if history else 0
            total_closed_pnl = sum(t.get("pnl", 0) for t in history)
            print(Fore.CYAN + "─" * 70)
            print(f" TRADES: {len(history)} | {Fore.GREEN}WINS: {len(wins)}{Style.RESET_ALL} | {Fore.RED}LOSS: {len(losses)}{Style.RESET_ALL} | Win Rate: {win_rate:.1f}%")
            pnl_color = Fore.GREEN if total_closed_pnl >= 0 else Fore.RED
            print(f" {pnl_color}{Style.BRIGHT}TOTAL REALIZED PNL: {total_closed_pnl:+.4f} USDT{Style.RESET_ALL}")

        # Recent Logs (New section)
        if _bot_logs:
            print(Fore.CYAN + "─" * 70)
            print(Fore.CYAN + Style.BRIGHT + " 📝 RECENT SYSTEM LOGS:")
            # Deque is thread-safe, but we can convert to list for slicing/reversing if needed
            for log in list(_bot_logs)[-8:]:
                print(f" {log}")

        # Trade History (last 100 closed trades)
        print(Fore.CYAN + "─" * 70)
        print(Fore.CYAN + Style.BRIGHT + f" 📜 TRADE HISTORY ({len(history)} total):")
        for t in reversed(history[-100:]):
            held_secs = t.get("hold_time_seconds", 0)
            m, s = divmod(int(held_secs), 60)
            p_color = Fore.GREEN if t.get("pnl", 0) > 0 else Fore.RED
            direction = t.get("direction", "?")
            print(f" {t['timestamp'][11:16]} {t['symbol']:<14} | {direction:5} | PnL: {p_color}{t.get('pnl', 0):+.4f} USDT{Style.RESET_ALL} | Held: {m}m {s}s")
            
        sys.stdout.flush()
        time.sleep(2)


# ────────────────────────────────────────────────────────────────────
# Instrument info cache (lot sizes)
# ────────────────────────────────────────────────────────────────────
_instrument_cache: Dict[str, dict] = {}
_instrument_loaded = False


def _load_instruments():
    global _instrument_loaded
    if _instrument_loaded: return
    data = _get("/public/products")
    if not data or data.get("code") != 0:
        logger.warning("Could not load instrument data — will use fallback qty rounding")
        _instrument_loaded = True
        return
    for prod in (data.get("data", {}).get("perpProductsV2") or []):
        sym = prod.get("symbol")
        if not sym: continue
        # qtyStepSize for lot sizing
        step_str = (
            prod.get("qtyStepSize") or 
            prod.get("qtyStepSizeRq") or 
            "0.001"
        )
        try: step = float(step_str)
        except Exception: step = 0.001
        _instrument_cache[sym] = {"step": step}
    _instrument_loaded = True
    logger.info("Loaded %d instrument specs", len(_instrument_cache))


def _round_qty(symbol: str, qty: float) -> str:
    """
    Round qty down to the instrument's lot step size.
    Falls back to 3 decimal places if instrument data unavailable.
    """
    _load_instruments()
    info = _instrument_cache.get(symbol)
    if info:
        step = info["step"]
        if step <= 0: step = 0.001
        rounded = math.floor(qty / step) * step
        # Determine decimal places from step
        if step >= 1:
            decimals = 0
        else:
            decimals = len(str(step).rstrip("0").split(".")[-1])
        return f"{rounded:.{decimals}f}"
    else:
        # Fallback: use 3 decimal places for most coins
        return f"{math.floor(qty * 1000) / 1000:.3f}"

def _get_current_price_rest(symbol: str) -> Optional[float]:
    """Fetches the current price of a symbol using the REST API."""
    path = "/md/v2/kline/list"
    params = {
        "symbol": symbol,
        "interval": "1m",
        "limit": 1
    }
    data = _get(path, params)
    if data and data.get("code") == 0 and data.get("data") and data["data"]["rows"]:
        try:
            # Phemex kline: [ts, interval, last_close, open, high, low, close, volume, turnover]
            # Close is at index 6
            return float(data["data"]["rows"][0][6]) 
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing REST API price for {symbol}: {e}")
    logger.debug(f"Could not get REST API price for {symbol}. Response: {data}")
    return None

# ────────────────────────────────────────────────────────────────────
# Account & position queries
# ────────────────────────────────────────────────────────────────────

def get_account_status() -> Tuple[Optional[float], List[dict]]:
    """Fetches both balance and open positions in a single API call."""
    data = _get("/g-accounts/accountPositions", {"currency": "USDT"})
    if not data or data.get("code") != 0:
        return None, []
    
    # Parse Balance
    balance = None
    try:
        bal_str = data["data"]["account"]["accountBalanceRv"]
        balance = float(bal_str)
    except Exception as e:
        logger.error("Balance parse error: %s", e)
        
    # Parse Positions
    positions = []
    for pos in (data.get("data", {}).get("positions") or []):
        try:
            size = float(pos.get("size") or "0")
        except Exception:
            size = 0.0
        if size == 0.0:
            continue
            
        positions.append({
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),  # "Buy" or "Sell"
            "size": size,
            "entry": float(pos.get("avgEntryPriceRp") or 0.0),
            "pnl": float(pos.get("unrealisedPnlRv") or 0.0),
            "pos_side": pos.get("posSide", "Merged"),
        })
        
    return balance, positions


def get_balance() -> Optional[float]:
    """Returns available USDT balance."""
    balance, _ = get_account_status()
    return balance


def get_open_positions() -> List[dict]:
    """
    Returns list of USDT-M positions that are actually open (size != 0).
    Each dict has: symbol, side (Buy/Sell), size (float), avgEntryPriceRp
    """
    _, positions = get_account_status()
    return positions


def get_recent_realized_pnl(symbol: str) -> float:
    """Fetch the realized PnL of the most recent closed trade for a symbol."""
    # This uses the Phemex Contract/Unified account data API
    # type=1 (REALIZED_PNL)
    params = {
        "currency": "USDT",
        "type": "1",
        "limit": "5",
    }
    data = _get("/api-data/futures/v2/tradeAccountDetail", params)
    if not data or data.get("code") != 0:
        return 0.0
    
    # The response 'data' field is actually a list, not a dict with 'rows'
    items = data.get("data", [])
    if not isinstance(items, list) or not items:
        return 0.0
        
    # If the API provides a symbol, use it to filter.
    # Otherwise, we'll take the most recent amount if it happened very recently (last 60s)
    # as a best-effort guess, but only if it's likely our trade.
    for item in items:
        item_symbol = item.get("symbol")
        ts = item.get("createTime", 0) / 1000
        
        # If symbol matches, we're sure
        if item_symbol == symbol:
            return float(item.get("amountRv") or 0.0)
            
        # If symbol is missing but it's very recent, take the first one
        # (This preserves the original 'best-effort' behavior but adds symbol check)
        if not item_symbol and time.time() - ts < 60:
            return float(item.get("amountRv") or 0.0)
            
    return 0.0


def symbols_in_position() -> set:
    return {p["symbol"] for p in get_open_positions()}


# ────────────────────────────────────────────────────────────────────
# Leverage setter
# ────────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int, pos_side: Optional[str] = None) -> bool:
    """
    Set leverage for a symbol before entry.
    Uses cross margin (leverageRr = leverage).
    For cross margin mode in Phemex USDT perps, pass positive leverage.
    The exchange's margin mode (cross vs isolated) is set in the account settings;
    this call only sets the leverage multiplier. If the requested leverage fails
    due to TE_ERR_INVALID_LEVERAGE, it attempts to set a lower default.
    """
    if pos_side is None:
        pos_side = "Merged" if POSITION_MODE == "OneWay" else "Long"
            
    # Attempt to set the requested leverage
    result = _put("/g-positions/leverage", params={
        "symbol": symbol,
        "leverageRr": str(leverage),
        "posSide": pos_side,
    })
    if result and result.get("code") == 0:
        logger.debug("Leverage set to %dx for %s", leverage, symbol)
        return True
    
    # If initial attempt fails with invalid leverage, try a lower value
    if isinstance(result, dict) and result.get("code") == 20003:
        fallback_leverage = 10  # Try 10x as a fallback
        logger.warning(f"TE_ERR_INVALID_LEVERAGE for {symbol} at {leverage}x. Retrying with {fallback_leverage}x.")
        
        result_fallback = _put("/g-positions/leverage", params={
            "symbol": symbol,
            "leverageRr": str(fallback_leverage),
            "posSide": pos_side,
        })
        if result_fallback and result_fallback.get("code") == 0:
            logger.info(f"Leverage successfully set to {fallback_leverage}x for {symbol} after fallback.")
            return True
        else:
            logger.error(f"Fallback leverage to {fallback_leverage}x also failed for {symbol}: {result_fallback}")
            return False
        
    logger.warning("Failed to set leverage for %s to %dx: %s", symbol, leverage, result)
    return False


def _switch_pos_mode(symbol: str, target_mode: str) -> bool:
    """
    Ensures the position mode for a symbol is set correctly.
    target_mode: 'BothSide' (Hedged) or 'MergedSingle' (One-Way)
    """
    path = "/g-positions/switch-pos-mode-sync"
    params = {
        "symbol": symbol,
        "targetPosMode": target_mode
    }
    result = _put(path, params=params)
    if result and result.get("code") == 0:
        logger.debug("Position mode for %s set to %s", symbol, target_mode)
        return True
    
    # If already in the correct mode, Phemex might return an error code like 20002 or 20004
    # We should handle that gracefully.
    if isinstance(result, dict) and result.get("code") in [20002, 20004, 34002, 10500]:
        # Note: Some APIs return 10500 if already in that mode, though 10500 is often 'invalid targetPosMode'
        # We'll rely on our updated target_mode values being correct.
        return True
        
    if isinstance(result, dict) and result.get("code") is not None:
        logger.error(f"Failed to set position mode for {symbol} to {target_mode}: {result.get('msg')} (Code {result.get('code')})")
    elif result is not None:
        logger.error("Failed to set position mode for %s to %s: Unexpected response format: %s", symbol, target_mode, result)
    else:
        logger.error("Failed to set position mode for %s to %s: No response from API", symbol, target_mode)
    return False


# ────────────────────────────────────────────────────────────────────
# Order placement
# ────────────────────────────────────────────────────────────────────

def _clord_id(prefix: str = "bot") -> str:
    import random
    ts = int(time.time() * 1000) % 1_000_000_000
    suffix = random.randint(1000, 9999)
    return f"{prefix}-{ts}-{suffix}"


def place_market_order(
    symbol: str,
    side: str,         # "Buy" or "Sell"
    qty_str: str,      # real quantity string e.g. "0.003"
    pos_side: Optional[str] = None,
    clord_id: Optional[str] = None, # New argument for client order ID
) -> Optional[dict]:
    """
    Place a market order on USDT-M perpetuals.
    Uses PUT /g-orders/create (preferred endpoint per docs).
    """
    clord = clord_id if clord_id else _clord_id("entry")
    if pos_side is None:
        if POSITION_MODE == "OneWay":
            pos_side = "Merged"
        else:
            pos_side = "Long" if side == "Buy" else "Short"
            
    params = {
        "clOrdID": clord,
        "symbol": symbol,
        "side": side,
        "ordType": "Market",
        "orderQtyRq": qty_str,
        "posSide": pos_side,
        "timeInForce": "ImmediateOrCancel",
    }
    result = _put("/g-orders/create", params=params)
    return result


def place_trailing_stop(
    symbol: str,
    side: str,          # "Sell" for long, "Buy" for short
    qty_str: str,
    price: float,       # entry/current price for initial stop calculation
    trail_pct: float = 0.005,
    pos_side: Optional[str] = None,
) -> Optional[dict]:
    """
    Place a trailing stop order that closes the entire position.
    From Phemex docs (USDT-M section):
        ordType = "Stop"
        pegPriceType = "TrailingStopPeg"
        pegOffsetValueRp: negative for long (Sell), positive for short (Buy)
        stopPxRp : initial trigger price
            long:  price * (1 - trail_pct) — must be < last price
            short: price * (1 + trail_pct) — must be > last price
        closeOnTrigger = true
        orderQtyRq = "0" (close entire position)
        triggerType = "ByLastPrice"
        timeInForce = "GoodTillCancel"
    """
    if pos_side is None:
        if POSITION_MODE == "OneWay":
            pos_side = "Merged"
        else:
            # For Hedged mode, if we are closing a Long position (side=Sell), posSide must be Long
            # If we are closing a Short position (side=Buy), posSide must be Short
            pos_side = "Long" if side == "Sell" else "Short"
            
    offset_amount = price * trail_pct
    
    if side == "Sell": # Closing a long position
        stop_px = price * (1.0 - trail_pct)
        peg_offset = f"{-offset_amount:.4f}" # negative = trail below for long
    else: # Closing a short position
        stop_px = price * (1.0 + trail_pct)
        peg_offset = f"{offset_amount:.4f}"  # positive = trail above for short
        
    clord = _clord_id("trail")
    result = _put("/g-orders/create", params={
        "clOrdID": clord,
        "symbol": symbol,
        "side": side,
        "ordType": "Stop",
        "orderQtyRq": qty_str,
        "stopPxRp": f"{stop_px:.4f}",
        "pegPriceType": "TrailingStopPeg",
        "pegOffsetValueRp": peg_offset,
        "triggerType": "ByLastPrice",
        "timeInForce": "GoodTillCancel",
        "closeOnTrigger": "true",
        "posSide": pos_side,
    })
    
    if result:
        logger.info(f"[TRAIL STOP] {symbol} response: code={result.get('code')} data={json.dumps(result.get('data', {}))[:200]}")
    else:
        logger.warning(f"[TRAIL STOP] {symbol} — no response from API")
    return result


# ────────────────────────────────────────────────────────────────────
# Cancel existing trailing stops (before re-placing)
# ────────────────────────────────────────────────────────────────────

def cancel_all_orders(symbol: str) -> bool:
    """Cancel all active + untriggered orders for a symbol."""
    # Cancel active orders
    r1 = _delete(
        "/g-orders/all",
        params={"symbol": symbol, "untriggered": "false"},
    )
    # Cancel untriggered conditional orders (trailing stops that haven't fired)
    r2 = _delete(
        "/g-orders/all",
        params={"symbol": symbol, "untriggered": "true"},
    )
    ok1 = r1 and r1.get("code") == 0
    ok2 = r2 and r2.get("code") == 0
    return ok1 or ok2

def cancel_order_by_client_id(symbol: str, client_order_id: str) -> bool:
    """Cancel a specific order using its client order ID."""
    result = _delete("/g-orders/cancel", params={"symbol": symbol, "clOrdID": client_order_id})
    if result and result.get("code") == 0:
        logger.info(f"Successfully cancelled order {client_order_id} for {symbol}.")
        return True
    else:
        logger.error(f"Failed to cancel order {client_order_id} for {symbol}: {result}")
        return False


# ────────────────────────────────────────────────────────────────────
# Trade logging
# ────────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()


def log_trade(entry: dict):
    with _log_lock:
        # Use JSON Lines format for efficient O(1) appending
        # This resolves T3-15 (O(n) I/O per trade)
        log_file_jsonl = BOT_LOG_FILE.with_suffix(".jsonl")
        try:
            with open(log_file_jsonl, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to append to trade log: {e}")
            
        # Also keep the JSON list for backward compatibility with dashboard
        trades = []
        if BOT_LOG_FILE.exists():
            try:
                trades = json.loads(BOT_LOG_FILE.read_text())
            except Exception:
                trades = []
        trades.append(entry)
        # Cap the JSON list to last 1000 trades to prevent O(n) growth issues
        if len(trades) > 1000:
            trades = trades[-1000:]
        BOT_LOG_FILE.write_text(json.dumps(trades, indent=2))


# ────────────────────────────────────────────────────────────────────
# Core execution: enter a setup
# ────────────────────────────────────────────────────────────────────

def verify_candidate(symbol: str, direction: str, original_score: int, wait_seconds: int = 15) -> Optional[dict]:
    """
    Waits, then re-scans a single symbol to verify the signal is still valid.
    """
    print(Fore.CYAN + f" [WAIT] Verifying {symbol} in {wait_seconds}s...")
    time.sleep(wait_seconds)
    
    # Fetch fresh ticker
    tickers = pc.get_tickers(rps=RATE_LIMIT_RPS)
    ticker = next((t for t in tickers if t["symbol"] == symbol), None)
    if not ticker:
        print(Fore.RED + f" [FAIL] {symbol} ticker not found during verification.")
        return None

    # Re-scan using the appropriate scanner module
    scanner = scanner_long if direction == "LONG" else scanner_short
    
    # Minimal config for re-scan
    cfg = {
        "TIMEFRAME": TIMEFRAME,
        "MIN_VOLUME": MIN_VOLUME,
        "RATE_LIMIT_RPS": RATE_LIMIT_RPS,
        "CANDLES": 100
    }
    
    # Scanner modules expect a 'cfg' dict and 'args' object (or mock)
    class MockArgs:
        def __init__(self):
            self.no_ai = True
            self.no_entity = True
    
    print(Fore.CYAN + f" [RE-SCAN] Re-analysing {symbol}...")
    fresh_result = scanner.analyse(ticker, cfg, enable_ai=False, enable_entity=False)
    
    if not fresh_result:
        print(Fore.RED + f" [FAIL] {symbol} no longer qualifies after wait.")
        return None
        
    fresh_score = fresh_result["score"]
    diff = fresh_score - original_score
    diff_str = f"{diff:+.0f}" if diff != 0 else "0"
    
    if fresh_score < original_score * 0.9: # Allow 10% score degradation
        print(Fore.RED + f" [FAIL] {symbol} score dropped too much: {original_score} -> {fresh_score}")
        return None
        
    print(Fore.GREEN + f" [VERIFIED] {symbol} score: {fresh_score} (Change: {diff_str})")
    return fresh_result

def execute_setup(result: dict, direction: str, dry_run: bool = False) -> bool:
    """
    Executes a trade setup on Phemex.
    
    This function performs the following steps:
    1. Validates the signal and checks against the blacklist.
    2. Determines leverage and margin based on liquidity and HTF alignment.
    3. Sets the symbol's leverage on the exchange.
    4. Places a market order for entry.
    5. Places a trailing stop order for risk management.
    6. Updates local state and logs the trade.
    
    Args:
        result (dict): The scanner result dictionary containing 'inst_id', 'price', 'score', and 'signals'.
        direction (str): The trade direction, either 'LONG' or 'SHORT'.
        dry_run (bool): If True, logs the actions but does not place real orders.
        
    Returns:
        bool: True if the setup was executed successfully (or dry-run), False otherwise.
    """
    symbol = result["inst_id"]
    price  = result["price"]
    score  = result["score"]
    signals = result.get("signals", [])
    
    if price <= 0:
        logger.warning("[%s] Invalid price %.4g — skipping", symbol, price)
        return False

    # ── Dynamic Cooldown Check ────────────────────────────────
    r, scalar = get_readiness_info(symbol, result, direction=direction)
    if r < 0.35:
        tui_log(f"SKIP: {symbol} is on cooldown (R={r:.2f} < 0.35)", event_type="SKIP")
        return False

    # ── Liquidity-Adjusted Parameters ──────────────────
    # Low-Liquidity assets: wider stop, lower leverage, smaller margin bet.
    # Log analysis: Low-Liq appears in 45% of losses vs 22% of wins.
    is_low_liq      = any("Low Liquidity" in s for s in signals)
    is_htf_aligned  = any("HTF Alignment" in s for s in signals)

    if is_low_liq:
        active_leverage  = LOW_LIQ_LEVERAGE
        active_trail_pct = LOW_LIQ_TRAIL_PCT
        active_margin    = LOW_LIQ_MARGIN
        liq_note = f"LOW-LIQ MODE: {active_leverage}x lev, {active_trail_pct*100:.1f}% stop, ${active_margin} margin"
        tui_log(f"{symbol}: {liq_note}", event_type="LOWLIQ")
    else:
        active_leverage  = LEVERAGE
        active_trail_pct = TRAIL_PCT
        active_margin    = MARGIN_USDT

    # ── Quality Gate ───────────────────────────────────
    # Fine-grained gate applied at execution time (pick_candidates uses coarse filter).
    # HTF aligned → lower bar (33% wins, 0% losses). Low-liq → higher bar.
    effective_min = MIN_SCORE_LOW_LIQ if is_low_liq else (
        MIN_SCORE_HTF_BYPASS if is_htf_aligned else MIN_SCORE_DEFAULT
    )

    if score < effective_min:
        gate_reason = "low-liq" if is_low_liq else ("htf-ok" if is_htf_aligned else "no-HTF")
        tui_log(f"SKIP: {symbol} score {score} < effective min {effective_min} ({gate_reason})", event_type="SKIP")
        return False

    # ── Compute quantity ─────────────────────────────────────────────
    # Apply Readiness Scalar from Dynamic Cooldown
    active_margin = active_margin * scalar
    if scalar < 1.0:
        tui_log(f"{symbol}: Scaling margin ${active_margin:.2f} (Scalar: {scalar:.2f}x)", event_type="COOLDOWN")

    notional = active_margin * active_leverage
    qty_raw  = notional / price
    qty_str  = _round_qty(symbol, qty_raw)
    
    if float(qty_str) <= 0:
        logger.warning("[%s] Qty rounds to 0 at price %.4g — skipping", symbol, price)
        return False
        
    side       = "Buy"  if direction == "LONG" else "Sell"
    close_side = "Sell" if direction == "LONG" else "Buy"
    arrow      = "▲ LONG" if direction == "LONG" else "▼ SHORT"
    dir_color  = Fore.GREEN if direction == "LONG" else Fore.RED
    
    pos_side = "Merged" if POSITION_MODE == "OneWay" else ("Long" if direction == "LONG" else "Short")

    tui_log(f"EXECUTING {arrow} {symbol} | Score: {score} | Price: {price:.4g} | Qty: {qty_str} | Margin: ${active_margin} | Lev: {active_leverage}x", event_type="EXEC")
    
    # --- Entity API: Trade Intent ---
    trade_id = f"tr-{symbol}-{int(time.time())}"
    make_entity_request("trade", data={
        "trade_id": trade_id,
        "symbol": symbol,
        "direction": direction,
        "status": "INITIATED",
        "entry": price,
        "parameters": {
            "leverage": active_leverage,
            "margin": active_margin,
            "trail_pct": active_trail_pct,
            "low_liq": is_low_liq
        },
        "scoring": {
            "raw": score,
            "effective": _effective_score(result)
        },
        "market_context": {
            "htf_aligned": is_htf_aligned,
            "signals": signals[:10]
        }
    })

    if dry_run:
        tui_log(f"DRY RUN — no orders placed for {symbol}", event_type="DRY")
        log_trade({
            "timestamp": datetime.datetime.now().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "qty": qty_str,
            "score": score,
            "dry_run": True,
            "status": "dry_run",
        })
        make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "DRY_RUN"})
        return True

    # ── Set position mode ───────────────────────────────────────────
    target_mode = "MergedSingle" if POSITION_MODE == "OneWay" else "BothSide"
    mode_ok = _switch_pos_mode(symbol, target_mode)
    if not mode_ok:
        print(Fore.RED + f" [ERROR] Failed to set position mode for {symbol} to {target_mode}. Cannot proceed.")
        return False

    # ── Set leverage ─────────────────────────────────────────────────
    lev_ok = set_leverage(symbol, active_leverage, pos_side)
    if not lev_ok:
        print(Fore.YELLOW + f" [WARN] Leverage set returned non-zero — proceeding anyway (check logs for details)")
        
    # Generate client order ID for market entry
    entry_clord_id = _clord_id("entry")

    # ── Place market entry ───────────────────────────────────────────
    tui_log(f"Placing {side} Market order for {symbol}...", event_type="ORDER")
    order_resp = place_market_order(symbol, side, qty_str, clord_id=entry_clord_id)
    
    if not order_resp:
        tui_log(f"ERROR: No response from order endpoint for {symbol}", event_type="ERROR")
        make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "ORDER_FAILED", "outcome": "NO_RESPONSE"})
        return False
        
    code = order_resp.get("code", -1)
    if code != 0:
        biz_err = order_resp.get("data", {}).get("bizError") if isinstance(order_resp.get("data"), dict) else None
        tui_log(f"ERROR: Order failed for {symbol}: code={code} bizError={biz_err}", event_type="ERROR")
        make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "ORDER_FAILED", "outcome": f"CODE_{code}"})
        return False
        
    order_id    = order_resp.get("data", {}).get("orderID", "?")
    exec_status = order_resp.get("data", {}).get("execStatus", "?")
    avg_price   = float(order_resp.get("data", {}).get("avgPriceRp") or price)
    
    # Entity API Hook: Order
    make_entity_request("order", data={
        "order_id": order_id,
        "trade_id": trade_id,
        "symbol": symbol,
        "type": "Market",
        "side": side,
        "qty_requested": float(qty_str),
        "qty_filled": float(order_resp.get("data", {}).get("cumQtyRq", qty_str)),
        "price_filled": avg_price,
        "status": exec_status,
        "submitted_at": datetime.datetime.now().isoformat(),
        "leverage": active_leverage,
        "pos_side": pos_side,
        "exchange_response_code": str(code)
    })
    make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "ENTERED", "entry": avg_price})

    tui_log(f"Entry order accepted | {symbol} | orderID: {order_id} | status: {exec_status} | Price: {avg_price:.6g}", event_type="ENTRY")

    # Telegram Alert
    emoji = "🚀" if direction == "LONG" else "📉"
    msg = (f"{emoji} *TRADE OPENED*\n\n"
           f"*Symbol:* {symbol}\n"
           f"*Direction:* {direction}\n"
           f"*Price:* {price:.4g}\n"
           f"*Score:* {score}\n"
           f"*Time:* {datetime.datetime.now().strftime('%H:%M:%S')}")
    send_telegram_message(msg)

    # ── Brief pause to let fill propagate ───────────────────────────
    time.sleep(1.5)
    
    # ── Place trailing stop (with retries) ──────────────────────────
    ts_ok = False
    ts_id = None
    max_ts_retries = 3
    for i in range(max_ts_retries):
        tui_log(f"Placing trailing stop ({active_trail_pct*100:.1f}%, {close_side}) for {symbol} attempt {i+1}/{max_ts_retries}...", event_type="STOP")
        ts_resp = place_trailing_stop(symbol, close_side, qty_str, price, active_trail_pct)
        
        if ts_resp and ts_resp.get("code") == 0:
            ts_id = ts_resp.get("data", {}).get("orderID", "?")
            tui_log(f"Trailing stop placed | {symbol} | orderID: {ts_id}", event_type="STOP")
            ts_ok = True
            
            # ── Now confirmed: Add to cached positions ────────────────────────
            with _cache_lock:
                # Deduplicate: only append if not already present (prevents race with _cache_refresher)
                if not any(p["symbol"] == symbol for p in _cached_positions):
                    _cached_positions.append({
                        "symbol": symbol,
                        "side": side,
                        "size": float(qty_str),
                        "entry": avg_price,
                        "pnl": 0.0,
                        "pos_side": pos_side,
                    })
            # Entity API Hook: Order (Stop)
            make_entity_request("order", data={
                "order_id": ts_id,
                "trade_id": trade_id,
                "symbol": symbol,
                "type": "Stop",
                "side": close_side,
                "qty_requested": 0, # Close all
                "qty_filled": 0,
                "status": "Untriggered",
                "submitted_at": datetime.datetime.now().isoformat(),
                "leverage": active_leverage,
                "pos_side": pos_side,
                "exchange_response_code": "0"
            })
            make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "MONITORING", "exit": 0})
            break # Exit retry loop on success
        else:
            ts_biz = ts_resp.get("data", {}).get("bizError") if isinstance(ts_resp.get("data"), dict) else None
            logger.warning(f"Trailing stop failed (attempt {i+1}): code={ts_resp.get('code')} bizError={ts_biz}. Retrying...")
            time.sleep(2) # Wait before retrying

    if not ts_ok:
        tui_log(f"CRITICAL ERROR: Failed to place trailing stop for {symbol} after {max_ts_retries} attempts.", event_type="CRITICAL")
        tui_log(f"Cancelling entry order {order_id} (clOrdID: {entry_clord_id}) to prevent unprotected position!", event_type="CRITICAL")
        make_entity_request("trade", method="PUT", entity_id=trade_id, data={"status": "STOP_FAILED_AND_CANCELLED", "outcome": "TRAIL_STOP_FAILURE"})
        
        # Note: We didn't append to _cached_positions yet, so no need to filter it out.
        
        if cancel_order_by_client_id(symbol, entry_clord_id): # entry_clord_id is the client order ID for the entry
            tui_log(f"Entry order {order_id} cancelled successfully for {symbol}.", event_type="CRITICAL")
            log_trade({
                "timestamp": datetime.datetime.now().isoformat(),
                "symbol": symbol,
                "direction": direction,
                "price": price,
                "qty": qty_str,
                "score": score,
                "dry_run": False,
                "status": "entry_cancelled",
                "reason": "trail_stop_failed"
            })
            return False # Entry failed
        else:
            tui_log(f"CRITICAL ERROR: Failed to cancel entry order {order_id}. MANUAL INTERVENTION REQUIRED for {symbol}!", event_type="CRITICAL")
            # Send an emergency telegram message
            send_telegram_message(f"🚨 *URGENT:* Failed to place trailing stop AND failed to cancel entry for {symbol}. Manual intervention needed!")
            return False # Entry failed, and cancellation also failed

    # If trailing stop was successfully placed

    if ts_ok:
        # Save local stop state for dashboard display
        offset = price * active_trail_pct
        if direction == "LONG":
            _local_stop_states[symbol] = {
                "stop_price": price - offset,
                "high_water": price,
                "entry_time": datetime.datetime.now(),
                "entry_score": score,
                "direction": direction,
            }
        else:
            _local_stop_states[symbol] = {
                "stop_price": price + offset,
                "low_water": price,
                "entry_time": datetime.datetime.now(),
                "entry_score": score,
                "direction": direction,
            }

    print(dir_color + Style.BRIGHT + f" {'─'*70}\n")
    
    log_trade({
        "timestamp": datetime.datetime.now().isoformat(),
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "qty": qty_str,
        "notional": round(float(qty_str) * price, 2),
        "margin_usdt": active_margin,
        "leverage": active_leverage,
        "trail_pct": active_trail_pct,
        "low_liq_mode": is_low_liq,
        "htf_aligned": is_htf_aligned,
        "score": score,
        "signals": result.get("signals", [])[:5],
        "entry_order_id": order_id,
        "trail_order_id": ts_id,
        "trail_ok": ts_ok,
        "dry_run": False,
        "status": "entered",
        "pnl": 0, # Entry has 0 realized PnL
    })
    return True


# ────────────────────────────────────────────────────────────────────
# Scan & decide
# ────────────────────────────────────────────────────────────────────

def run_scanner_both(cfg: dict, args, on_result=None) -> Tuple[List[dict], List[dict]]:
    """Run both scanners (no printing), return (long_results, short_results)."""
    import concurrent.futures
    
    def _scan(module, direction):
        return _scan_one(module, direction, cfg, args, on_result=on_result)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as exe:
        fut_long  = exe.submit(_scan, scanner_long,  "LONG")
        fut_short = exe.submit(_scan, scanner_short, "SHORT")
        res_long  = fut_long.result()
        res_short = fut_short.result()
    
    return res_long, res_short


_print_lock = threading.Lock()

def _scan_one(module, direction: str, cfg: dict, args, on_result=None) -> List[dict]:
    rps = cfg.get("RATE_LIMIT_RPS", 8.0)
    
    # Batch pre-fetch funding rates to save hundreds of API calls
    if hasattr(module, "prefetch_all_funding_rates"):
        module.prefetch_all_funding_rates(rps=rps)
        
    tickers = module.get_tickers(rps=rps)
    
    # Filter by symbols if provided
    symbols_to_scan = cfg.get("SYMBOLS")
    if symbols_to_scan:
        filtered = [t for t in tickers if t.get("symbol") in symbols_to_scan]
        logger.info(f"Filtered to {len(filtered)} symbols from request")
    else:
        filtered = [
            t for t in tickers 
            if (lambda v: v >= cfg["MIN_VOLUME"])(float(t.get("turnoverRv") or 0.0))
        ]
    
    results = []
    total = len(filtered)
    done = 0
    if total == 0: return []
    
    import concurrent.futures
    workers = min(cfg["MAX_WORKERS"], max(1, len(filtered)))
    
    # Concurrent execution
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(module.analyse, t, cfg, not args.no_ai, not args.no_entity, None) for t in filtered]
        for fut in concurrent.futures.as_completed(futures):
            try:
                r = fut.result()
                if r:
                    r["scan_timestamp"] = datetime.datetime.now()
                    results.append(r)
                    if on_result:
                        threading.Thread(target=on_result, args=(r, direction), daemon=True).start()
            except Exception: pass
            done += 1
                
    return results


def _effective_score(result: dict) -> float:
    signals = result.get("signals", [])
    base    = result.get("score", 0)
    bonus   = 15 if any("HTF Alignment" in s for s in signals) else 0
    penalty = 20 if any("Low Liquidity" in s for s in signals) else 0
    return base + bonus - penalty


def pick_candidates(
    long_results, short_results,
    min_score, min_score_gap,
    direction_filter, in_position,
    available_slots,
) -> list:
    symbol_scores = {}
    for r in long_results:
        symbol_scores.setdefault(r["inst_id"], {"LONG": 0, "SHORT": 0})["LONG"] = r["score"]
    for r in short_results:
        symbol_scores.setdefault(r["inst_id"], {"LONG": 0, "SHORT": 0})["SHORT"] = r["score"]

    candidates = []

    if direction_filter.upper() in ["LONG", "BOTH"]:
        for r in long_results:
            if r["score"] < min_score: continue
            if r["inst_id"] in in_position: continue
            scores = symbol_scores.get(r["inst_id"], {"LONG": 0, "SHORT": 0})
            if scores["LONG"] - scores["SHORT"] < min_score_gap: continue
            candidates.append((r, "LONG"))

    if direction_filter.upper() in ["SHORT", "BOTH"]:
        for r in short_results:
            if r["score"] < min_score: continue
            if r["inst_id"] in in_position: continue
            scores = symbol_scores.get(r["inst_id"], {"LONG": 0, "SHORT": 0})
            if scores["SHORT"] - scores["LONG"] < min_score_gap: continue
            candidates.append((r, "SHORT"))

    # Sort by quality-adjusted score, not raw
    candidates.sort(key=lambda x: _effective_score(x[0]), reverse=True)
    return candidates[:available_slots]


# ────────────────────────────────────────────────────────────────────
# Print helpers
# ────────────────────────────────────────────────────────────────────

def print_positions(positions: List[dict]):
    if not positions:
        print(Fore.WHITE + " No open positions.")
        return
    for p in positions:
        pnl = p.get("pnl", 0.0)
        pnl_color = Fore.GREEN if pnl >= 0 else Fore.RED
        side_color = Fore.GREEN if p["side"] == "Buy" else Fore.RED
        
        # Duration info from _local_stop_states
        dur_str = ""
        if p["symbol"] in _local_stop_states:
            ls = _local_stop_states[p["symbol"]]
            et = ls.get("entry_time")
            if et:
                diff = datetime.datetime.now() - et
                tot_sec = int(diff.total_seconds())
                if tot_sec < 60: dur_str = f"({tot_sec}s)"
                elif tot_sec < 3600: dur_str = f"({tot_sec//60}m)"
                else: dur_str = f"({tot_sec//3600}h {(tot_sec%3600)//60}m)"
        
        print(
            f"  {side_color}{'▲' if p['side']=='Buy' else '▼'} {p['symbol']:<16}{Style.RESET_ALL}"
            f" Size: {p['size']}  Entry: {p['entry']:.4g} "
            f" PnL: {pnl_color}{pnl:+.4f} USDT{Style.RESET_ALL} {Fore.WHITE}{dur_str}{Style.RESET_ALL}"
        )


def print_candidates(candidates: List[Tuple[dict, str]]):
    if not candidates:
        print(Fore.YELLOW + " No candidates pass min-score or available slots.")
        return
    for r, direction in candidates:
        dir_color = Fore.GREEN if direction == "LONG" else Fore.RED
        from phemex_short import grade
        g, gc = grade(r["score"])
        print(
            f"  {dir_color}{'▲' if direction=='LONG' else '▼'} {r['inst_id']:<16}{Style.RESET_ALL}"
            f" Score: {gc}{r['score']}{Style.RESET_ALL} ({g}) "
            f" Price: {r['price']:.4g} "
            f" RSI: {r.get('rsi') or 0:.1f} "
            f" Funding: {(r.get('funding_pct') or 0):+.4f}%"
        )


# ────────────────────────────────────────────────────────────────────
# Main bot loop
# ────────────────────────────────────────────────────────────────────

# ── Cluster & Entropy Tracking (Idea 2 & 3) ─────────────────────────
_hawkes_long = pc.HawkesTracker(mu=0.1, alpha=0.8, beta=0.1)
_hawkes_short = pc.HawkesTracker(mu=0.1, alpha=0.8, beta=0.1)
_entropy_penalty = 0

def _get_cluster_threshold_penalty(intensity: float) -> int:
    """Returns a score penalty based on Hawkes intensity (λ)."""
    if intensity > 3.0: return 50  # Major cluster - raise bar significantly
    if intensity > 2.0: return 30
    if intensity > 1.2: return 15
    return 0

def bot_loop(args):
    global _account_high_water, _account_trail_stop, _account_trading_halted, _cached_balance, _cached_positions, _entropy_penalty
    
    cfg = {
        "MIN_VOLUME": args.min_vol,
        "TIMEFRAME":  args.timeframe,
        "CANDLES":    100,
        "TOP_N":      50,    # scan wide, filter later
        "MIN_SCORE":  0,     # don't filter in scanner, we'll do it here
        "MAX_WORKERS": args.workers,
        "RATE_LIMIT_RPS": args.rate,
    }

    # Initial account state
    _cached_balance = get_balance() or 0.0
    _cached_positions = get_open_positions()
    _account_high_water = _cached_balance + sum([p.get("pnl", 0.0) for p in _cached_positions])
    _account_trail_stop = _account_high_water * (1 - ACCOUNT_TRAIL_PCT)

    load_blacklist() # Load persistent blacklist at startup

    # Load recent trade history for recovery
    history = []
    if BOT_LOG_FILE.exists():
        try: history = json.loads(BOT_LOG_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to load trade history at bot startup: {e}")

    # Start WebSocket, Dashboard and Cache Refresher
    _ensure_ws_started()
    for p in _cached_positions:
        _subscribe_symbol(p["symbol"])
        
    # Populate local stop state for existing positions so they can be tracked
    for p in _cached_positions:
        if p["symbol"] not in _local_stop_states:
            # Find the most recent 'entered' status for this symbol in history
            h_entry = next((h for h in reversed(history) if h.get("symbol") == p["symbol"] and h.get("status") == "entered"), None)
            entry_time = datetime.datetime.now()
            entry_score = 0
            if h_entry:
                try:
                    entry_time = datetime.datetime.fromisoformat(h_entry["timestamp"])
                    entry_score = h_entry.get("score", 0)
                except Exception as e:
                    logger.warning(f"Failed to parse entry timestamp for {p['symbol']} from history: {e}")
                
            _local_stop_states[p["symbol"]] = {
                "stop_price": 0, # Unknown initially
                "entry_time": entry_time,
                "entry_score": entry_score,
                "direction": "LONG" if p["side"] == "Buy" else "SHORT",
            }
            
    global _display_thread_running
    if not _display_thread_running:
        _display_thread_running = True
        threading.Thread(target=_live_pnl_display, daemon=True).start()
        threading.Thread(target=_cache_refresher, daemon=True).start()

    scan_number = 0
    while True:
        if _account_trading_halted:
            time.sleep(30)
            continue
            
        scan_number += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Account status ───────────────────────────────────────────
        with _cache_lock:
            in_pos = {p["symbol"] for p in _cached_positions}
            # Dynamic scaling: more positions as equity grows
            dynamic_max = get_dynamic_max_positions(_cached_balance)
            available_slots = dynamic_max - len(_cached_positions)
            balance = _cached_balance

        # ── Fast-track callback ──────────────────────────────────────
        def on_scan_result(r, direction, args=args):
            if _account_trading_halted: 
                return
            
            # ── Hawkes Cluster Throttling (Idea 3) ────────────────────
            tracker = _hawkes_long if direction == "LONG" else _hawkes_short
            intensity = tracker.update(event_occurred=False) # Decay only
            hawkes_penalty = _get_cluster_threshold_penalty(intensity)
            
            # Use global _entropy_penalty from last scan to block cascades
            effective_fast_track = FAST_TRACK_SCORE + hawkes_penalty + _entropy_penalty
            if r["score"] < effective_fast_track:
                if (hawkes_penalty > 0 or _entropy_penalty > 0) and r["score"] >= FAST_TRACK_SCORE:
                    tui_log(f"FT THROTTLE: {r['inst_id']} score {r['score']} < dynamic FT threshold {effective_fast_track} (λ={intensity:.2f}, H_pen={_entropy_penalty})", event_type="THROTTLE")
                return

            # Signal passed the throttle, now we pulse the tracker to reflect this event
            intensity = tracker.update(event_occurred=True)
            hawkes_penalty = _get_cluster_threshold_penalty(intensity) # Update penalty for subsequent checks

            # Staleness check
            result_time = r.get("scan_timestamp")
            if result_time and (datetime.datetime.now() - result_time).total_seconds() > RESULT_STALENESS_SECONDS:
                return

            with _fast_track_lock:
                with _cache_lock:
                    # Account for positions already open PLUS those currently being verified
                    if len(_cached_positions) + len(_fast_track_opened) >= get_dynamic_max_positions(_cached_balance): 
                        return
                    if r["inst_id"] in {p["symbol"] for p in _cached_positions}: 
                        return
                
                if r["inst_id"] in _fast_track_opened: return
                
                # Cooldown check
                last_ft = FAST_TRACK_COOLDOWN.get(r["inst_id"], 0)
                if time.time() - last_ft < FAST_TRACK_COOLDOWN_SECONDS: 
                    return
                
                # ── Readiness Check for Fast-Track ────────────────────
                # If symbol just stopped out, we require higher conviction (R >= 0.70)
                # than the standard scan path (R >= 0.35) to allow immediate re-entry.
                curr_r, _ = get_readiness_info(r["inst_id"], r)
                if curr_r < 0.70 and is_blacklisted(r["inst_id"]):
                    tui_log(f"FT BLOCK: {r['inst_id']} readiness R={curr_r:.2f} < 0.70 threshold for Fast-Track re-entry", event_type="COOLDOWN")
                    return

                _fast_track_opened.add(r["inst_id"])
                FAST_TRACK_COOLDOWN[r["inst_id"]] = time.time()
                
            try:
                tui_log(f"FAST-TRACK: {r['inst_id']} scored {r['score']} — opening immediately (λ={intensity:.2f})", event_type="FAST")
                
                # Entity API Hook: Fast Track signal
                make_entity_request("signalevent", data={
                    "signal_id": f"sig-{r['inst_id']}-{int(time.time())}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "symbol": r["inst_id"],
                    "direction": direction,
                    "raw_score": r["score"],
                    "effective_score": _effective_score(r),
                    "passed_quality_gate": True,
                    "executed": True,
                    "skip_reason": "FAST_TRACK",
                    "hawkes_intensity": intensity,
                    "entropy_penalty": _entropy_penalty
                })

                # ── Wait & Verify ────────────────────────────────────
                # Release lock during verify_candidate (15s sleep) and setup
                verified_result = verify_candidate(r["inst_id"], direction, r["score"])
                
                if verified_result:
                    ok = execute_setup(verified_result, direction, dry_run=args.dry_run)
                    if ok:
                        _subscribe_symbol(r["inst_id"])
                        time.sleep(2)
            finally:
                with _fast_track_lock:
                    if r["inst_id"] in _fast_track_opened:
                        _fast_track_opened.remove(r["inst_id"])

        # ── Scan ─────────────────────────────────────────────────────
        if _cached_balance < LOW_LIQ_MARGIN: # allow entry even in low-liq mode ($5 min)
             # Wait if balance is critical
             time.sleep(args.interval)
             continue
             
        if available_slots <= 0:
            time.sleep(args.interval)
            continue

        _display_paused.set() # pause dashboard during scan output to avoid mess
        long_r, short_r = run_scanner_both(cfg, args, on_result=on_scan_result)
        _display_paused.clear()

        # ── Cross-Asset Entropy Deflator (Idea 2) ─────────────────────
        # Only count meaningful signals (Score >= 100) to avoid false-positive cascades
        n_hits = len([r for r in long_r if r["score"] >= 100]) + len([r for r in short_r if r["score"] >= 100])
        
        all_tickers = pc.get_tickers(rps=args.rate)
        total_universe = len([t for t in all_tickers if float(t.get("turnoverRv", 0)) >= args.min_vol])
        
        imbalance = 0.0
        if total_universe > 0 and n_hits > 0:
            # Saturation: percentage of universe firing
            sat_ratio = n_hits / total_universe
            # Adjusted: Hit ~25 points at 20% saturation, capped at 40
            sat_penalty = min(40, int(sat_ratio * 125))
            
            # One-sidedness: how imbalanced are the signals?
            total_n_hits = len(long_r) + len(short_r)
            imbalance = abs(len(long_r) - len(short_r)) / total_n_hits if total_n_hits > 0 else 0.0
            side_penalty = int(25 * imbalance)
            
            _entropy_penalty = sat_penalty + side_penalty
        else:
            _entropy_penalty = 0
            
        if _entropy_penalty > 15:
            tui_log(f"ENTROPY DEFLATOR: Raising min_score by +{_entropy_penalty} (Saturation: {n_hits}/{total_universe}, Imbalance: {imbalance:.2f})", event_type="DEFLATOR")

        # ── Dynamic threshold calculation ─────────────────────────────
        eff_min_score = args.min_score + _entropy_penalty
        if not args.no_dynamic:
            all_scores = [r["score"] for r in (long_r + short_r)]
            dynamic_min = pc.calc_dynamic_threshold(all_scores, args.min_score)
            eff_min_score = max(eff_min_score, dynamic_min)
            
        if eff_min_score > args.min_score:
            print(Fore.YELLOW + f" [ADAPTIVE FILTER] Effective Min Score: {eff_min_score} (Penalty: +{_entropy_penalty})")

        # ── Staleness filter ──────────────────────────────────────────
        fresh_long  = [r for r in long_r  if (datetime.datetime.now() - r.get("scan_timestamp", datetime.datetime.now())).total_seconds() < RESULT_STALENESS_SECONDS]
        fresh_short = [r for r in short_r if (datetime.datetime.now() - r.get("scan_timestamp", datetime.datetime.now())).total_seconds() < RESULT_STALENESS_SECONDS]

        # ── Pick candidates ──────────────────────────────────────
        with _cache_lock:
            in_pos_updated    = {p["symbol"] for p in _cached_positions}
            available_updated = get_dynamic_max_positions(_cached_balance) - len(_cached_positions)
            
        candidates = pick_candidates(
            fresh_long, fresh_short, 
            min_score=eff_min_score, 
            min_score_gap=args.min_score_gap,
            direction_filter=args.direction,
            in_position=in_pos_updated, 
            available_slots=available_updated,
        )

        # ── Execute ──────────────────────────────────────────────
        sleep_interval = args.interval
        if candidates:
            for result, direction in candidates:
                with _cache_lock:
                    # Recheck available slots before executing each candidate,
                    # as a fast-track or manual action might have filled a slot
                    if len(_cached_positions) >= get_dynamic_max_positions(_cached_balance):
                        make_entity_request("signalevent", data={
                            "signal_id": f"sig-{result['inst_id']}-{int(time.time())}",
                            "timestamp": datetime.datetime.now().isoformat(),
                            "symbol": result["inst_id"],
                            "direction": direction,
                            "raw_score": result["score"],
                            "effective_score": _effective_score(result),
                            "passed_quality_gate": True,
                            "executed": False,
                            "skip_reason": "MAX_POSITIONS_AFTER_SCAN_CANDIDATE"
                        })
                        print(Fore.YELLOW + f" [SKIP] {result['inst_id']} - Max positions reached while processing candidates.")
                        continue # Skip this candidate
                
                # Entity API Hook: Execute Candidate
                make_entity_request("signalevent", data={
                    "signal_id": f"sig-{result['inst_id']}-{int(time.time())}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "symbol": result["inst_id"],
                    "direction": direction,
                    "raw_score": result["score"],
                    "effective_score": _effective_score(result),
                    "passed_quality_gate": True,
                    "executed": True,
                    "skip_reason": "SCAN_CANDIDATE"
                })

                # ── Wait & Verify ────────────────────────────────────
                # scan -> select -> wait -> verify -> enter
                verified_result = verify_candidate(result["inst_id"], direction, result["score"])
                if not verified_result:
                    continue # Verification failed, skip to next candidate
                
                ok = execute_setup(verified_result, direction, dry_run=args.dry_run)
                if ok:
                    _subscribe_symbol(result["inst_id"])
                    time.sleep(2)
        else:
            # If no candidates, scan more frequently
            logger.info("No qualifying candidates found. Shortening scan interval.")
            sleep_interval = 5 # Check every 5 seconds if nothing is found

        # ── Sleep ────────────────────────────────────────────────────
        _slot_available_event.wait(timeout=sleep_interval)
        _slot_available_event.clear()


# ────────────────────────────────────────────────────────────────────
# One-shot mode: single scan + execute
# ────────────────────────────────────────────────────────────────────

def one_shot(args):
    """Run a single scan, print results, and optionally execute top setup."""
    cfg = {
        "MIN_VOLUME": args.min_vol,
        "TIMEFRAME":  args.timeframe,
        "CANDLES":    100,
        "TOP_N":      50,
        "MIN_SCORE":  0,
        "MAX_WORKERS": args.workers,
        "RATE_LIMIT_RPS": args.rate,
    }
    
    load_blacklist() # Load persistent blacklist at startup
    
    balance   = get_balance()
    positions = get_open_positions()
    in_pos    = {p["symbol"] for p in positions}
    
    print(Fore.CYAN + Style.BRIGHT + f"\n{'='*70}")
    print(Fore.CYAN + Style.BRIGHT + " 🔍 ONE-SHOT SCAN")
    print(Fore.CYAN + Style.BRIGHT + f"{'='*70}")
    print(f" Balance: {f'{balance:.2f}' if balance is not None else '?'} USDT | "
          f"Positions: {len(positions)}/{MAX_POSITIONS}\n")
    
    print_positions(positions)
    
    print(Fore.WHITE + f"\n Running scanners ({args.timeframe})...")
    long_r, short_r = run_scanner_both(cfg, args)
    
    # ── Dynamic threshold ─────────────────────────────────────────────
    eff_min_score = args.min_score
    if not args.no_dynamic:
        all_scores = [r["score"] for r in (long_r + short_r)]
        eff_min_score = pc.calc_dynamic_threshold(all_scores, args.min_score)
        if eff_min_score > args.min_score:
            print(Fore.YELLOW + f" [ADAPTIVE FILTER] Dynamic Min Score: {eff_min_score}")

    available_slots = MAX_POSITIONS - len(positions)
    candidates = pick_candidates(
        long_r, short_r, 
        min_score=eff_min_score, 
        min_score_gap=args.min_score_gap,
        direction_filter=args.direction,
        in_position=in_pos, 
        available_slots=available_slots
    )
    
    print(f"\n Scan complete — Longs: {len(long_r)}  Shorts: {len(short_r)}")
    print(f" Candidates (score ≥ {eff_min_score}): {len(candidates)}\n")
    
    print_candidates(candidates)
    
    if candidates and not args.dry_run:
        print()
        confirm = input(Fore.YELLOW + " Execute top candidate? [y/N]: ").strip().lower()
        if confirm == "y":
            top_result, top_dir = candidates[0]
            # ── Wait & Verify ────────────────────────────────────
            verified_result = verify_candidate(top_result["inst_id"], top_dir, top_result["score"])
            if verified_result:
                execute_setup(verified_result, top_dir, dry_run=False)
            else:
                print(Fore.RED + " [FAIL] Verification failed — entry cancelled.")
        else:
            print(Fore.YELLOW + " Skipped.")
    elif candidates and args.dry_run:
        print()
        for r, d in candidates:
            # ── Wait & Verify (DRY RUN) ──────────────────────────
            verified_result = verify_candidate(r["inst_id"], d, r["score"])
            if verified_result:
                execute_setup(verified_result, d, dry_run=True)
            else:
                print(Fore.RED + f" [FAIL] {r['inst_id']} failed verification.")


# ────────────────────────────────────────────────────────────────────
# Status command
# ────────────────────────────────────────────────────────────────────

def show_status():
    print(Fore.CYAN + Style.BRIGHT + f"\n {'='*60}")
    print(Fore.CYAN + Style.BRIGHT + "  BOT STATUS")
    print(Fore.CYAN + Style.BRIGHT + f" {'='*60}")
    print(f" Exchange  : {BASE_URL}")
    print(f" API Key   : {API_KEY[:8]}..." if API_KEY else " API Key   : NOT SET ⚠")
    print(f" Margin    : ${MARGIN_USDT} @ {LEVERAGE}x = ${MARGIN_USDT*LEVERAGE:.0f} notional")
    print(f" Trail     : {TRAIL_PCT*100:.1f}%")
    print(f" Max Pos   : {MAX_POSITIONS}")
    print(f" Min Score : {MIN_SCORE}")
    print()
    
    balance, positions = get_account_status()
    balance_display = f"{balance:.4f}" if balance is not None else "ERROR"
    print(f" Balance   : {balance_display} USDT")
    
    print(f" Positions ({len(positions)}/{MAX_POSITIONS}):")
    print_positions(positions)
    
    # Recent trades
    if BOT_LOG_FILE.exists():
        try:
            trades = json.loads(BOT_LOG_FILE.read_text())
            print(f"\n Recent trades ({len(trades)} total):")
            for t in trades[-5:][::-1]:
                dr = "DRY" if t.get("dry_run") else "LIVE"
                print(f"  {t['timestamp'][:19]} {t.get('direction','?'):5} "
                      f"{t['symbol']:<16} Score:{t['score']} "
                      f"@{t['price']:.4g} [{dr}]")
        except Exception: pass
    print()


# ────────────────────────────────────────────────────────────────────
# One-shot deploy mode: single scan + exit
# ────────────────────────────────────────────────────────────────────

def deploy_once(args):
    """Run a single scan, execute top candidates, and exit."""
    cfg = {
        "MIN_VOLUME": args.min_vol,
        "TIMEFRAME":  args.timeframe,
        "CANDLES":    100,
        "TOP_N":      50,
        "MIN_SCORE":  0,
        "MAX_WORKERS": args.workers,
        "RATE_LIMIT_RPS": args.rate,
    }
    
    load_blacklist() # Load persistent blacklist at startup
    
    # Refresh current status
    balance   = get_balance()
    positions = get_open_positions()
    in_pos    = {p["symbol"] for p in positions}
    available_slots = MAX_POSITIONS - len(positions)
    
    print(Fore.CYAN + Style.BRIGHT + f"\n{'='*70}")
    print(Fore.CYAN + Style.BRIGHT + " 🚀 ONE-SHOT DEPLOY")
    print(Fore.CYAN + Style.BRIGHT + f"{'='*70}")
    print(f" Balance: {balance:.2f} USDT | Available slots: {available_slots}/{MAX_POSITIONS}\n")
    
    if available_slots <= 0:
        print(Fore.YELLOW + " All position slots filled — exiting.")
        return
        
    print(Fore.WHITE + f" Running scanners ({args.timeframe})...")
    long_r, short_r = run_scanner_both(cfg, args)
    
    # ── Dynamic threshold ─────────────────────────────────────────────
    eff_min_score = args.min_score
    if not args.no_dynamic:
        all_scores = [r["score"] for r in (long_r + short_r)]
        eff_min_score = pc.calc_dynamic_threshold(all_scores, args.min_score)
        if eff_min_score > args.min_score:
            print(Fore.YELLOW + f" [ADAPTIVE FILTER] Dynamic Min Score: {eff_min_score}")

    candidates = pick_candidates(
        long_r, short_r, 
        min_score=eff_min_score, 
        min_score_gap=args.min_score_gap,
        direction_filter=args.direction,
        in_position=in_pos, 
        available_slots=available_slots,
    )
    
    print(f"\n Scan complete — Longs: {len(long_r)}  Shorts: {len(short_r)}")
    print(f" Qualifying Candidates (score ≥ {eff_min_score}): {len(candidates)}\n")
    
    print_candidates(candidates)
    
    if not candidates:
        print(Fore.YELLOW + " No candidates found — exiting.")
        return
        
    opened_count = 0
    deployed_summary = []
    
    print()
    for result, direction in candidates:
        if opened_count >= available_slots: break
        
        # ── Wait & Verify ────────────────────────────────────
        verified_result = verify_candidate(result["inst_id"], direction, result["score"])
        if not verified_result:
            continue
            
        ok = execute_setup(verified_result, direction, dry_run=args.dry_run)
        if ok:
            opened_count += 1
            # Retrieve last stop from _local_stop_states for summary
            stop_info = _local_stop_states.get(result["inst_id"], {})
            stop_price = stop_info.get("stop_price", 0)
            
            deployed_summary.append({
                "symbol": result["inst_id"],
                "dir": direction,
                "price": result["price"],
                "stop": stop_price,
                "score": result["score"]
            })
            time.sleep(2)
            
    if deployed_summary:
        msg_header = "🔥 *DEPLOYMENT COMPLETE*\n\n"
        msg_lines = []
        for s in deployed_summary:
            emoji = "▲" if s["dir"] == "LONG" else "▼"
            msg_lines.append(f"{emoji} *{s['symbol']}* @ {s['price']:.4g} (Stop: {s['stop']:.4g}) | Score: {s['score']}")
        msg = msg_header + "\n".join(msg_lines)
        send_telegram_message(msg)
        print(Fore.GREEN + Style.BRIGHT + "\n ✅ Deployment summary sent to Telegram.")
        
    print(Fore.CYAN + "\n Deployment task finished. Exiting.\n")


def verify_trailing_stops():
    """
    Verifies if trailing stop orders are present by querying the Phemex API.
    This function is intended for post-deployment verification.
    """
    from dotenv import load_dotenv
    load_dotenv(override=True) # Ensure @.env is loaded in this context and overrides existing env vars

    # Read credentials locally — never mutate module-level API_KEY / API_SECRET
    local_key    = os.getenv("PHEMEX_API_KEY", API_KEY)
    local_secret = os.getenv("PHEMEX_API_SECRET", API_SECRET)
    local_url    = os.getenv("PHEMEX_BASE_URL", BASE_URL)

    print("--- Verifying Trailing Stop Orders ---")
    print(f"Using API Key: {local_key[:8]}...")
    print(f"Base URL: {local_url}")

    r = _get('/g-orders/activeList', {'currency': 'USDT', 'ordStatus': 'Untriggered'})
    if r and r.get('code') == 0:
        print("Verification successful. Active Untriggered Orders:")
        print(json.dumps(r.get('data', {}), indent=2))

        trailing_stops = [
            order for order in r.get('data', {}).get('rows', [])
            if order.get('ordType') == 'Stop' and order.get('pegPriceType') == 'TrailingStopPeg'
        ]

        if trailing_stops:
            print("\n--- Found TrailingStopPeg Orders ---")
            print(json.dumps(trailing_stops, indent=2))
        else:
            print("\n--- No TrailingStopPeg Orders Found ---")
    else:
        print("Verification failed or no active untriggered orders found.")
        if r:
            print(f"API Response Code: {r.get('code')}, Message: {r.get('msg')}")
        else:
            print("No response from API.")
    print("------------------------------------")


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY or not API_SECRET:
        print(Fore.RED + "[ERROR] PHEMEX_API_KEY and PHEMEX_API_SECRET must be set in .env")
        print(" Example .env:")
        print("  PHEMEX_API_KEY=your-key-id")
        print("  PHEMEX_API_SECRET=your-secret")
        print("  PHEMEX_BASE_URL=https://testnet-api.phemex.com")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Phemex Automated Trading Bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command")
    
    # ── run: continuous loop ─────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run the bot in a continuous scan loop")
    run_p.add_argument("--interval",  type=int,   default=SCAN_INTERVAL, help="Seconds between scans")
    run_p.add_argument("--min-score", type=int,   default=MIN_SCORE, help="Min score to execute")
    run_p.add_argument("--min-score-gap", type=int, default=MIN_SCORE_GAP, help="Min score gap (Long - Short) for entry")
    run_p.add_argument("--direction", default=DIRECTION, choices=["LONG", "SHORT", "BOTH"], help="Direction to trade")
    run_p.add_argument("--timeframe", default=TIMEFRAME, help="Scanner timeframe")
    run_p.add_argument("--cooldown", type=int, default=4, help="Cooldown in candles after exit")
    run_p.add_argument("--min-vol",   type=int,   default=MIN_VOLUME, help="Min 24h USDT volume")
    run_p.add_argument("--workers",   type=int,   default=MAX_WORKERS, help="Scanner threads")
    run_p.add_argument("--rate",      type=float, default=RATE_LIMIT_RPS, help="API requests/sec")
    run_p.add_argument("--dry-run",   action="store_true", help="Don't place real orders")
    run_p.add_argument("--yes",       action="store_true", help="Skip mainnet confirmation")
    run_p.add_argument("--no-ai",     action="store_true", help="Disable AI commentary")
    run_p.add_argument("--no-entity", action="store_true", help="Disable Entity API")
    run_p.add_argument("--no-dynamic", action="store_true", help="Disable adaptive score filtering")

    # ── deploy: single scan + exit ───────────────────────────────────
    deploy_p = sub.add_parser("deploy", help="Run one scan and optionally execute, then exit")
    deploy_p.add_argument("--min-score", type=int,   default=MIN_SCORE)
    deploy_p.add_argument("--min-score-gap", type=int, default=MIN_SCORE_GAP)
    deploy_p.add_argument("--direction", default=DIRECTION, choices=["LONG", "SHORT", "BOTH"])
    deploy_p.add_argument("--timeframe", default=TIMEFRAME)
    deploy_p.add_argument("--cooldown", type=int, default=4)
    deploy_p.add_argument("--min-vol",   type=int,   default=MIN_VOLUME)
    deploy_p.add_argument("--workers",   type=int,   default=MAX_WORKERS)
    deploy_p.add_argument("--rate",      type=float, default=RATE_LIMIT_RPS)
    deploy_p.add_argument("--dry-run",   action="store_true", help="Print orders but don't execute")
    deploy_p.add_argument("--no-ai",     action="store_true")
    deploy_p.add_argument("--no-entity", action="store_true")
    deploy_p.add_argument("--no-dynamic", action="store_true")

    # ── once: single scan ───────────────────────────────────────────
    once_p = sub.add_parser("once", help="Run one scan and optionally execute")
    once_p.add_argument("--min-score", type=int,   default=MIN_SCORE)
    once_p.add_argument("--min-score-gap", type=int, default=MIN_SCORE_GAP)
    once_p.add_argument("--direction", default=DIRECTION, choices=["LONG", "SHORT", "BOTH"])
    once_p.add_argument("--timeframe", default=TIMEFRAME)
    once_p.add_argument("--cooldown", type=int, default=4)
    once_p.add_argument("--min-vol",   type=int,   default=MIN_VOLUME)
    once_p.add_argument("--workers",   type=int,   default=MAX_WORKERS)
    once_p.add_argument("--rate",      type=float, default=RATE_LIMIT_RPS)
    once_p.add_argument("--dry-run",   action="store_true", help="Print orders but don't execute")
    once_p.add_argument("--no-ai",     action="store_true")
    once_p.add_argument("--no-entity", action="store_true")
    once_p.add_argument("--no-dynamic", action="store_true")

    # ── status ──────────────────────────────────────────────────────
    sub.add_parser("status", help="Show account balance, open positions, and recent trades")

    # ── verify-stops ────────────────────────────────────────────────
    sub.add_parser("verify-stops", help="Verify trailing stop orders on the exchange")

    args = parser.parse_args()

    # Update global blacklist duration based on timeframe and cooldown if in run/deploy/once
    if args.command in ["run", "deploy", "once"]:
        global BLACKLIST_DURATION_SECONDS
        tf_sec = get_tf_seconds(args.timeframe)
        BLACKLIST_DURATION_SECONDS = args.cooldown * tf_sec
        logger.info(f"Cooldown set to {BLACKLIST_DURATION_SECONDS}s ({args.cooldown} candles)")

    # Entity API: Start Session
    make_entity_request("botsession", data={
        "session_id": SESSION_ID,
        "started_at": datetime.datetime.now().isoformat(),
        "config": {
            "margin": MARGIN_USDT,
            "leverage": LEVERAGE,
            "trail_pct": TRAIL_PCT,
            "min_score": args.min_score if hasattr(args, "min_score") else MIN_SCORE,
            "min_score_gap": args.min_score_gap if hasattr(args, "min_score_gap") else MIN_SCORE_GAP,
            "direction": args.direction if hasattr(args, "direction") else DIRECTION,
            "timeframe": args.timeframe if hasattr(args, "timeframe") else TIMEFRAME
        },
        "status": "STARTED"
    })

    if args.command == "status":
        show_status()
    elif args.command == "verify-stops":
        verify_trailing_stops()
    elif args.command == "once":
        one_shot(args)
    elif args.command == "deploy":
        testnet = "testnet" in BASE_URL
        env_label = Fore.YELLOW + "⚠ TESTNET" if testnet else Fore.RED + "🚨 MAINNET — REAL MONEY"
        print(Fore.CYAN + Style.BRIGHT + f"\n 🚀 Phemex ONE-SHOT DEPLOY Starting")
        print(f" Exchange  : {env_label}{Style.RESET_ALL} ({BASE_URL})")
        print(f" Margin    : ${MARGIN_USDT} @ {LEVERAGE}x = ${MARGIN_USDT*LEVERAGE:.0f} notional")
        print(f" Trail     : {TRAIL_PCT*100:.1f}% | Max Positions: {MAX_POSITIONS}")
        print(f" Min Score : {args.min_score} | Min Gap: {args.min_score_gap} | Direction: {args.direction}")
        if args.dry_run:
            print(Fore.YELLOW + " MODE      : DRY RUN — no real orders will be placed")
        
        try:
            deploy_once(args)
            make_entity_request("botsession", method="PUT", entity_id=SESSION_ID, data={
                "ended_at": datetime.datetime.now().isoformat(),
                "ended_reason": "DEPLOY_FINISHED",
                "status": "FINISHED"
            })
        except KeyboardInterrupt:
            print(Fore.YELLOW + "\n Deployment stopped by user.")
            make_entity_request("botsession", method="PUT", entity_id=SESSION_ID, data={
                "ended_at": datetime.datetime.now().isoformat(),
                "ended_reason": "USER_INTERRUPT",
                "status": "INTERRUPTED"
            })
            
    elif args.command == "run":
        testnet = "testnet" in BASE_URL
        env_label = Fore.YELLOW + "⚠ TESTNET" if testnet else Fore.RED + "🚨 MAINNET — REAL MONEY"
        
        print(Fore.CYAN + Style.BRIGHT + f"\n 🤖 Phemex Trading Bot Starting")
        print(f" Exchange  : {env_label}{Style.RESET_ALL} ({BASE_URL})")
        print(f" Margin    : ${MARGIN_USDT} @ {LEVERAGE}x = ${MARGIN_USDT*LEVERAGE:.0f} notional")
        print(f" Trail     : {TRAIL_PCT*100:.1f}% | Max Positions: {MAX_POSITIONS}")
        print(f" Interval  : {args.interval}s | Min Score: {args.min_score}")
        print(f" Min Gap   : {args.min_score_gap} | Direction: {args.direction}")
        if args.dry_run:
            print(Fore.YELLOW + " MODE      : DRY RUN — no real orders will be placed")

        print()
        
        try:
            bot_loop(args)
        except KeyboardInterrupt:
            print(Fore.YELLOW + "\n\n Bot stopped by user. Shutting down...")
            if _ws_app:
                try:
                    _ws_app.close()
                except Exception:
                    pass
            make_entity_request("botsession", method="PUT", entity_id=SESSION_ID, data={
                "ended_at": datetime.datetime.now().isoformat(),
                "ended_reason": "USER_INTERRUPT",
                "status": "INTERRUPTED"
            })
            print(Fore.YELLOW + " Shutdown complete.")
    else:
        parser.print_help()
        print(f"\n Configured exchange: {BASE_URL}")
        print(f" API key present    : {'YES' if API_KEY else 'NO'}\n")


if __name__ == "__main__":
    main()
