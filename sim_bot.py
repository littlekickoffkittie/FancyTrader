#!/usr/bin/env python3
"""
Phemex Simulation (Paper Trading) Bot
======================================
Runs on LIVE production market data but simulates all trades locally.
Maintains a local 'paper_account.json' to track balance and positions.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform != "win32":
    import select
    import termios
    import tty

import blessed
import requests
import websocket
from colorama import Fore, Style, init
from dotenv import load_dotenv

import phemex_common as pc
import phemex_long as scanner_long
import phemex_short as scanner_short
import animations

# Safely import p_bot
try:
    import p_bot
except ImportError:
    print(Fore.RED + "CRITICAL: 'p_bot.py' not found. This module is required for risk parameters.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & Constants
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR          = Path(__file__).parent

# Initialize colorama for TUI colors
init(autoreset=True)

PAPER_ACCOUNT_FILE  = SCRIPT_DIR / "paper_account.json"
SIM_COOLDOWN_FILE   = SCRIPT_DIR / "sim_cooldowns.json"
INITIAL_BALANCE     = float(os.getenv("INITIAL_BALANCE", "100.0"))
TAKER_FEE_RATE      = pc.TAKER_FEE  # Use common constant (0.06%)

# Telegram
TG_CHAT_ID          = os.getenv("TG_CHAT_ID", "")
TG_BOT_TOKEN        = os.getenv("TG_BOT_TOKEN", "")

# Fast-track entry: fire immediately when score exceeds threshold
FAST_TRACK_SCORE            = pc.SCORE_FAST_TRACK
FAST_TRACK_COOLDOWN_SECONDS = 300   # seconds before same symbol can fast-track again
RESULT_STALENESS_SECONDS    = 120   # discard scan results older than this

# Per-symbol re-entry cooldown (4 candles × 4H = 16 hours)
COOLDOWN_SECONDS = 4 * 4 * 3600

# Exit signal configuration
EXIT_SIGNAL_SCORE_THRESHOLD = 100
EXIT_SIGNAL_SCAN_INTERVAL    = 60   # seconds between opposite signal checks
LAST_EXIT_SCAN_TIME: Dict[str, float] = {}

# Unicode Block Elements U+2581–U+2588 (8 chars; index math in sparkline() depends on count=8)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────

_live_prices:  Dict[str, float] = {}
_prices_lock  = threading.Lock()
_cooldown_lock = threading.Lock()
_stop_lock     = threading.Lock()
_log_lock      = threading.Lock()
_display_lock  = threading.Lock()
_fast_track_lock = threading.Lock()
_file_io_lock  = threading.Lock() # Lock for paper_account.json access

_ws_app:    Optional[websocket.WebSocketApp] = None
_ws_thread: Optional[threading.Thread]       = None

_slot_available_event  = threading.Event()
_display_paused        = threading.Event()
_display_thread_running = False

_rolling_stats = {"wins": 0, "losses": 0, "win_pnl": 0.0, "loss_pnl": 0.0}

FAST_TRACK_COOLDOWN: Dict[str, float] = {}  # symbol → timestamp of last fast-track
_fast_track_opened:  set[str]         = set()
_stop_contexts:      Dict[str, pc.StopContext] = {} # symbol → StopContext

# TUI log buffer
_bot_logs: deque[str] = deque(maxlen=100)

# Equity sparkline history
_equity_history: List[float] = []
_max_history     = 50

# Per-position PnL history — keyed by symbol
_pnl_histories: Dict[str, list] = {}
_pnl_lock = threading.Lock()

# Braille dot patterns for 2x4 resolution per cell
BRAILLE_MAP = [
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80],
]

def _to_braille(left_row: int, right_row: int) -> str:
    """Convert two column bit patterns into a braille unicode char."""
    bits = 0
    for row in range(4):
        if left_row & (1 << row):  bits |= BRAILLE_MAP[row][0]
        if right_row & (1 << row): bits |= BRAILLE_MAP[row][1]
    return chr(0x2800 + bits)

def render_pnl_chart(
    pnl_history: list,      # list of floats, e.g. [-0.5, -0.3, 0.1, 0.4, 0.8]
    width: int  = 40,       # character width of chart
    height: int = 8,        # character height of chart
    label: str  = "",       # e.g. "ENAUSDT"
    term  = None,           # blessed terminal instance
    y: int = 0,             # screen row to render at
    x: int = 0,             # screen col to render at
) -> list:
    """
    Renders a smooth braille PnL line chart.
    Returns list of strings (one per row) — print them or pass term for positioned render.
    """
    if not pnl_history:
        pnl_history = [0.0]

    # Pad or trim to fit width*2 data points (2 per char cell)
    points = pnl_history[-(width * 2):]
    while len(points) < width * 2:
        points = [points[0]] * (width * 2 - len(points)) + points

    lo  = min(points)
    hi  = max(points)
    span = (hi - lo) or 1e-10
    rows = height * 4  # braille gives 4 vertical dots per char row

    # Map each data point to a row index 0..rows-1
    def to_row(v):
        return int((v - lo) / span * (rows - 1))

    scaled = [to_row(p) for p in points]

    # Build the 2D braille grid
    grid = [[[0, 0] for _ in range(width)] for _ in range(height)]

    for col_idx in range(width):
        left_val  = scaled[col_idx * 2]
        right_val = scaled[col_idx * 2 + 1]

        for val, side in [(left_val, 0), (right_val, 1)]:
            char_row  = height - 1 - (val // 4)
            dot_row   = val % 4
            char_row  = max(0, min(height - 1, char_row))
            grid[char_row][col_idx][side] |= (1 << dot_row)

    # Render rows into strings
    zero_char_row = height - 1 - (to_row(0.0) // 4)
    lines = []

    for row_idx in range(height):
        line = ""
        for col_idx in range(width):
            l, r = grid[row_idx][col_idx]
            line += _to_braille(l, r)
        lines.append(line)

    current_pnl = pnl_history[-1]
    if term:
        chart_color  = term.bright_green if current_pnl >= 0 else term.red
        zero_color   = term.yellow
        label_color  = term.cyan
        reset        = term.normal
    else:
        chart_color  = Fore.LIGHTGREEN_EX if current_pnl >= 0 else Fore.RED
        zero_color   = Fore.YELLOW
        label_color  = Fore.CYAN
        reset        = Style.RESET_ALL

    output_lines = []

    # Top label bar
    pnl_str  = f"{current_pnl:+.4f} USDT"
    hi_str   = f"▲ {hi:+.4f}"
    lo_str   = f"▼ {lo:+.4f}"
    top_bar  = f"{label:<14} {pnl_str:>14}  {hi_str}  {lo_str}"

    output_lines.append(label_color + top_bar + reset)

    # Chart rows
    for row_idx, line in enumerate(lines):
        prefix = "│"
        suffix = "│"

        if row_idx == zero_char_row:
            output_lines.append(
                zero_color + prefix + reset +
                chart_color + line + reset +
                zero_color + suffix + reset +
                f" 0.00"
            )
        else:
            output_lines.append(zero_color + prefix + reset + chart_color + line + reset + zero_color + suffix + reset)

    # Bottom axis
    axis = "└" + "─" * width + "┘"
    time_label = "  entry" + " " * (width - 14) + "now  "
    output_lines.append(zero_color + axis + reset)
    output_lines.append(label_color + time_label + reset)

    # Render to screen if term provided
    if term:
        for i, l_content in enumerate(output_lines):
            print(term.move_xy(x, y + i) + l_content)
    else:
        for l_content in output_lines:
            print(l_content)

    return output_lines

def update_pnl_history(symbol: str, current_pnl: float):
    """Adds a new PnL data point to the history for the given symbol."""
    with _pnl_lock:
        if symbol not in _pnl_histories:
            _pnl_histories[symbol] = []
        _pnl_histories[symbol].append(current_pnl)
        # Keep last 200 data points
        _pnl_histories[symbol] = _pnl_histories[symbol][-200:]

# ── Logging Setup ─────────────────────────────────────────────────────
# Use the shared colored logging setup from phemex_common with buffer capture
logger = pc.setup_colored_logging(
    "sim_bot", 
    level=logging.INFO, 
    log_file=Path(SCRIPT_DIR) / "sim_bot.log",
    buffer=_bot_logs
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def tui_log(msg: str, event_type: str = "SIM") -> None:
    """Logs a message to both the system audit log and the TUI buffer."""
    pc.log_system_event(event_type, msg)
    # Ensure it also goes into our local logger which is hooked to the TUI deque
    logger.info(msg)


def play_animation(anim_fn):
    """Safely plays a cinematic animation by pausing the TUI thread."""
    _display_paused.set()
    time.sleep(0.5) # Let TUI finish its last frame
    animations.clear()
    try:
        anim_fn()
    finally:
        animations.clear()
        _display_paused.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram_message(message: str) -> None:
    """Sends a message to the configured Telegram chat."""
    try:
        url     = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram message: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Paper Account Management
# ─────────────────────────────────────────────────────────────────────────────

def capture_stop_context(symbol: str, price: float, is_stop_loss: bool = True, direction: str = "SHORT") -> pc.StopContext:
    """Captures ATR and Spread at the moment of exit for dynamic cooldown tracking."""
    # 1. ATR (use 4H timeframe by default as per p_bot)
    try:
        candles = pc.get_candles(symbol, timeframe=p_bot.TIMEFRAME, limit=100)
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
    with _cooldown_lock:
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
        spread=spread,
        direction=direction,
        stop_count=new_count
    )


def load_paper_account() -> dict:
    """Loads the paper account, creating it with defaults if it doesn't exist."""
    if not PAPER_ACCOUNT_FILE.exists():
        data = {"balance": INITIAL_BALANCE, "positions": []}
        save_paper_account(data)
        return data
    
    # Retry loop to handle concurrent read/writes
    for attempt in range(5):
        try:
            with _file_io_lock:
                content = PAPER_ACCOUNT_FILE.read_text()
                if not content:
                    raise ValueError("Empty file")
                return json.loads(content)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            if attempt == 4:
                logger.error(f"Failed to decode paper_account.json after {attempt+1} attempts — reinitializing.")
                return {"balance": INITIAL_BALANCE, "positions": []}
            time.sleep(0.05 * (attempt + 1))
    return {"balance": INITIAL_BALANCE, "positions": []}


def save_paper_account(data: dict) -> None:
    """Persists the current paper account state to disk."""
    with _file_io_lock:
        try:
            # Write to a temporary file first then rename to ensure atomicity
            temp_file = PAPER_ACCOUNT_FILE.with_suffix(".tmp")
            temp_file.write_text(json.dumps(data, indent=2))
            temp_file.replace(PAPER_ACCOUNT_FILE)
        except Exception as e:
            logger.error(f"Failed to save paper account: {e}")


def _close_all_positions() -> None:
    """Manually closes every active paper position at the current market price."""
    acc = load_paper_account()
    if not acc["positions"]:
        print(Fore.YELLOW + "  No positions to close.")
        return

    print(Fore.CYAN + f"  Closing {len(acc['positions'])} positions...")
    
    # --- Kill Cinematic ---
    play_animation(animations.kill)

    for pos in acc["positions"]:
        symbol = pos["symbol"]
        side   = pos["side"]
        entry  = pos["entry"]
        size   = float(pos["size"])

        with _prices_lock:
            now = _live_prices.get(symbol)

        if now is None:
            try:
                ticker = pc.get_tickers()
                now = next((float(t["lastRp"]) for t in ticker if t["symbol"] == symbol), entry)
            except Exception:
                now = entry

        pnl = (now - entry) * size if side == "Buy" else (entry - now) * size
        acc["balance"] += (pos.get("margin", 0.0) + pnl)

        with _cooldown_lock:
            # Manual closure counts as a neutral event (not a stop loss)
            _stop_contexts[symbol] = capture_stop_context(
                symbol, now, is_stop_loss=False, direction="LONG" if side == "Buy" else "SHORT"
            )

        pnl_emoji = "✅" if pnl > 0 else "❌"
        send_telegram_message(
            f"⏹ *SIM TRADES MANUALLY CLOSED (V2)*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Side:* {side}\n"
            f"*Exit Price:* {now}\n"
            f"*PnL:* {pnl_emoji} {pnl:+.4f} USDT\n"
            f"*Time:* {datetime.datetime.now().strftime('%H:%M:%S')}"
        )

        _log_closed_trade(
            symbol, side, entry, now, size,
            pos.get("entry_score", 0), pos.get("entry_time"), "manual_all_v2"
        )
        print(Fore.GREEN + f"  Closed {symbol} at {now}")

    acc["positions"] = []
    save_paper_account(acc)
    save_sim_cooldowns()
    _slot_available_event.set()
    print(Fore.GREEN + Style.BRIGHT + "  All positions closed successfully.")


def save_sim_cooldowns() -> None:
    """Persists active re-entry and fast-track cooldowns to disk, pruning expired entries."""
    with _cooldown_lock:
        # Keep contexts for 48 hours as a safety pruning limit if R doesn't reach 0.95
        active_exit = {
            s: ctx.__dict__ for s, ctx in _stop_contexts.items() 
            if time.time() - ctx.timestamp < 48 * 3600
        }
    with _fast_track_lock:
        active_ft = {s: ts for s, ts in FAST_TRACK_COOLDOWN.items() if time.time() - ts < FAST_TRACK_COOLDOWN_SECONDS}
    
    data = {
        "last_exit": active_exit,
        "fast_track": active_ft
    }
    try:
        SIM_COOLDOWN_FILE.write_text(json.dumps(data))
    except OSError:
        logger.error("Failed to save simulation cooldowns.")


def load_sim_cooldowns() -> None:
    """Loads re-entry and fast-track cooldowns from disk and discards any that have expired."""
    global _stop_contexts, FAST_TRACK_COOLDOWN
    if not SIM_COOLDOWN_FILE.exists():
        return
    try:
        data = json.loads(SIM_COOLDOWN_FILE.read_text())
        # Support old format (just exit times) and new format (dict with keys)
        if isinstance(data, dict) and "last_exit" in data and "fast_track" in data:
            exit_data = data["last_exit"]
            ft_data   = data["fast_track"]
        else:
            exit_data = data
            ft_data   = {}

        with _cooldown_lock:
            _stop_contexts = {}
            for s, val in exit_data.items():
                if isinstance(val, dict):
                    # New format: StopContext dict
                    if time.time() - val["timestamp"] < 48 * 3600:
                        _stop_contexts[s] = pc.StopContext(**val)
                else:
                    # Legacy format: float timestamp
                    if time.time() - float(val) < COOLDOWN_SECONDS:
                        # Create a mock StopContext for legacy entries
                        _stop_contexts[s] = pc.StopContext(
                            timestamp=float(val), price=0.0, atr=0.0, spread=0.05
                        )
        with _fast_track_lock:
            FAST_TRACK_COOLDOWN = {
                s: float(ts) for s, ts in ft_data.items()
                if time.time() - float(ts) < FAST_TRACK_COOLDOWN_SECONDS
            }
        logger.info(f"Loaded {len(_stop_contexts)} exit and {len(FAST_TRACK_COOLDOWN)} fast-track cooldowns.")
    except (json.JSONDecodeError, ValueError, AttributeError):
        logger.error("Failed to load simulation cooldowns — JSON is invalid.")


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket & Live Price Feed
# ─────────────────────────────────────────────────────────────────────────────

def _ws_on_message(ws: websocket.WebSocketApp, message: str) -> None:
    """Handles inbound WebSocket messages and updates the live price cache."""
    try:
        data = json.loads(message)
        if "market24h_p" in data:
            tick   = data["market24h_p"]
            symbol = tick.get("symbol")
            close  = tick.get("closeRp")
            if symbol and close is not None:
                with _prices_lock:
                    _live_prices[symbol] = float(close)
                _check_stops_live(symbol)
    except json.JSONDecodeError as e:
        logger.debug(f"WS message parse error: {e}")


def _ws_on_open(ws: websocket.WebSocketApp) -> None:
    """Subscribes to all currently open positions on WebSocket connect."""
    logger.info("WebSocket connection opened.")
    acc     = load_paper_account()
    symbols = [p["symbol"] for p in acc.get("positions", [])]
    if symbols:
        ws.send(json.dumps({"id": 1, "method": "market24h_p.subscribe", "params": symbols}))


def _ws_heartbeat(ws: websocket.WebSocketApp, stop_event: threading.Event) -> None:
    """Keeps the WebSocket alive by sending periodic pings."""
    while not stop_event.is_set():
        time.sleep(5)
        # Check if this heartbeat instance is still the active one
        if ws is not _ws_app:
            logger.debug("Heartbeat thread detected stale WS app — exiting.")
            break
        try:
            if ws.sock and ws.sock.connected:
                ws.send(json.dumps({"id": 0, "method": "server.ping", "params": []}))
            else:
                # Exit if socket is no longer connected
                break
        except (websocket.WebSocketConnectionClosedException, BrokenPipeError):
            logger.debug("WebSocket closed during heartbeat — exiting heartbeat thread.")
            break
        except Exception as e:
            logger.debug(f"Heartbeat error: {e}")
            break


def _ws_run_loop() -> None:
    """Maintains the WebSocket connection, reconnecting while positions are open."""
    global _ws_app
    ws_url = "wss://testnet.phemex.com/ws" if "testnet" in pc.BASE_URL else "wss://ws.phemex.com"

    retries = 0
    while True:
        stop_event = threading.Event()
        _ws_app = websocket.WebSocketApp(ws_url, on_message=_ws_on_message, on_open=_ws_on_open)
        threading.Thread(target=_ws_heartbeat, args=(_ws_app, stop_event), daemon=True).start()
        _ws_app.run_forever()
        
        # Signal heartbeat to stop after run_forever exits
        stop_event.set()

        # Grace period to allow pending saves/subscriptions to complete
        time.sleep(2.0)
        if not load_paper_account().get("positions"):
            break
        
        retries += 1
        delay = min(2**retries, 60)
        logger.info(f"WebSocket disconnected. Retrying in {delay}s (attempt {retries})...")
        time.sleep(delay)


def _ensure_ws_started() -> None:
    """Starts the WebSocket thread if it is not already running."""
    global _ws_thread
    if _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=_ws_run_loop, daemon=True)
        _ws_thread.start()


def _subscribe_symbol(symbol: str) -> None:
    """Subscribes the WebSocket to a new symbol after a short delay."""
    def _do_sub() -> None:
        time.sleep(1.5)
        if _ws_app and _ws_app.sock and _ws_app.sock.connected:
            symbols = [p["symbol"] for p in load_paper_account().get("positions", [])]
            _ws_app.send(json.dumps({"id": 1, "method": "market24h_p.subscribe", "params": symbols}))

    threading.Thread(target=_do_sub, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Stop / Take-Profit Monitoring
# ─────────────────────────────────────────────────────────────────────────────

def _check_stops_live(symbol: str) -> None:
    """Evaluates trailing-stop and take-profit levels for a symbol on each price tick."""
    # Narrow lock scope — copy data then release lock
    exit_to_process = None
    with _stop_lock:
        acc       = load_paper_account()
        positions = acc.get("positions", [])
        pos_idx   = next((i for i, p in enumerate(positions) if p["symbol"] == symbol), None)
        if pos_idx is None:
            return

        pos = positions[pos_idx]
        with _prices_lock:
            current_price = _live_prices.get(symbol)
        if current_price is None:
            return

        side  = pos["side"]
        entry = pos["entry"]
        size  = float(pos["size"])
        # Use .get() for stop_price and check existence
        stop_price = pos.get("stop_price")
        if stop_price is None:
            return

        stop_hit   = False
        tp_hit     = False
        exit_price = current_price

        if side == "Buy":
            if current_price > pos.get("high_water", 0.0):
                pos["high_water"]  = current_price
                pos["stop_price"]  = current_price * (1.0 - p_bot.TRAIL_PCT)
            if current_price <= stop_price:
                stop_hit   = True
                exit_price = stop_price
            elif "take_profit" in pos and current_price >= pos["take_profit"]:
                tp_hit     = True
                exit_price = pos["take_profit"]
        else:
            if current_price < pos.get("low_water", 9_999_999.0):
                pos["low_water"]  = current_price
                pos["stop_price"] = current_price * (1.0 + p_bot.TRAIL_PCT)
            if current_price >= stop_price:
                stop_hit   = True
                exit_price = stop_price
            elif "take_profit" in pos and current_price <= pos["take_profit"]:
                tp_hit     = True
                exit_price = pos["take_profit"]

        if not (stop_hit or tp_hit):
            save_paper_account(acc)
            return

        exit_reason = "Stop Hit" if stop_hit else "Take Profit Hit"
        pnl = (exit_price - entry) * size if side == "Buy" else (entry - exit_price) * size
        acc["balance"] += (pos.get("margin", 0.0) + pnl)
        
        # Prepare for I/O outside the lock
        exit_to_process = {
            "symbol": symbol,
            "side": side,
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "pnl": pnl,
            "entry": entry,
            "size": size,
            "entry_score": pos.get("entry_score", 0),
            "entry_time": pos.get("entry_time"),
            "stop_hit": stop_hit
        }
        
        positions.pop(pos_idx)
        save_paper_account(acc)

    # Process I/O outside the lock
    if exit_to_process:
        with _cooldown_lock:
            _stop_contexts[exit_to_process["symbol"]] = capture_stop_context(
                exit_to_process["symbol"], exit_to_process["exit_price"],
                is_stop_loss=exit_to_process["stop_hit"],
                direction="LONG" if exit_to_process["side"] == "Buy" else "SHORT"
            )
        save_sim_cooldowns()
        _slot_available_event.set()

        tui_log(f"{exit_to_process['exit_reason'].upper()} HIT: {symbol} {exit_to_process['side']} closed at {exit_to_process['exit_price']}", event_type="EXIT")

        pnl_emoji = "✅" if exit_to_process["pnl"] > 0 else "❌"
        
        # --- Exit Cinematic ---
        if exit_to_process["pnl"] > 10.0:  # Big win threshold
            play_animation(animations.big_win)
        elif exit_to_process["pnl"] > 0:
            play_animation(animations.win)
        else:
            play_animation(animations.loss)

        # Duration
        hold_time = 0
        if exit_to_process["entry_time"]:
            try:
                hold_time = (datetime.datetime.now() - datetime.datetime.fromisoformat(exit_to_process["entry_time"])).total_seconds()
            except Exception as e:
                logger.warning(f"Failed to parse entry time for {symbol}: {e}")
        h_min, h_sec = divmod(int(hold_time), 60)
        h_hour, h_min = divmod(h_min, 60)
        dur_str = f"{h_hour}h {h_min}m" if h_hour > 0 else (f"{h_min}m {h_sec}s" if h_min > 0 else f"{h_sec}s")

        send_telegram_message(
            f"🔔 *SIM TRADE CLOSED ({exit_to_process['exit_reason']})*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Side:* {exit_to_process['side']}\n"
            f"*Exit Price:* {exit_to_process['exit_price']}\n"
            f"*PnL:* {pnl_emoji} {exit_to_process['pnl']:+.4f} USDT\n"
            f"*Duration:* {dur_str}\n"
            f"*Time:* {datetime.datetime.now().strftime('%H:%M:%S')}"
        )
        _log_closed_trade(
            symbol, exit_to_process["side"], exit_to_process["entry"], 
            exit_to_process["exit_price"], exit_to_process["size"],
            exit_to_process["entry_score"], exit_to_process["entry_time"],
            "stop" if exit_to_process["stop_hit"] else "tp"
        )


def _log_closed_trade(
    symbol: str,
    direction: str,
    entry: float,
    exit_price: float,
    size: float,
    entry_score: float,
    entry_time: Optional[str],
    reason: str,
) -> None:
    """Appends a closed-trade record to sim_trade_results.json."""
    results_file = SCRIPT_DIR / "sim_trade_results.json"
    pnl = (exit_price - entry) * size if direction == "Buy" else (entry - exit_price) * size

    hold_time = 0
    if entry_time:
        try:
            hold_time = (datetime.datetime.now() - datetime.datetime.fromisoformat(entry_time)).total_seconds()
        except ValueError:
            logger.error("Invalid entry_time format — using zero hold time.")

    record = {
        "symbol":      symbol,
        "direction":   "LONG" if direction == "Buy" else "SHORT",
        "entry":       entry,
        "exit":        exit_price,
        "pnl":         round(pnl, 4),
        "hold_time_s": int(hold_time),
        "score":       entry_score,
        "reason":      reason,
        "timestamp":   datetime.datetime.now().isoformat(),
    }

    history: List[dict] = []
    with _file_io_lock:
        if results_file.exists():
            try:
                history = json.loads(results_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.error("Failed to read trade history — starting fresh.")
        history.append(record)
        results_file.write_text(json.dumps(history, indent=2))

    # --- Update rolling stats for Kelly ---
    global _rolling_stats
    if pnl > 0:
        _rolling_stats["wins"] += 1
        _rolling_stats["win_pnl"] += pnl
    else:
        _rolling_stats["losses"] += 1
        _rolling_stats["loss_pnl"] += pnl

    m, s = divmod(int(hold_time), 60)
    tui_log(
        f"CLOSED {symbol} | Entry: {entry}  Exit: {exit_price} | "
        f"PnL: {pnl:+.4f} USDT | Held: {m}m {s}s | Score: {entry_score}",
        event_type="HISTORY"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TUI — Drawing Helpers  (v2)
# ─────────────────────────────────────────────────────────────────────────────

# ── String helpers ────────────────────────────────────────────────────────────

def _vlen(s: str) -> int:
    """Visible length of a string — strips ANSI/escape codes."""
    return len(re.sub(r"\x1b\[[0-9;]*[mKHJ]", "", s))


def _rpad(s: str, width: int, char: str = " ") -> str:
    """Right-pad a styled string to exact visible `width`."""
    return s + char * max(0, width - _vlen(s))


# ── Box/panel primitives ──────────────────────────────────────────────────────

def _box_top(title: str, width: int, right_tag: str = "") -> str:
    """Single-line top border: ┌─ TITLE ──────────── right_tag ┐"""
    inner = width - 2
    if title:
        lbl = f"─ {title} "
        if right_tag:
            gap = inner - _vlen(lbl) - _vlen(right_tag) - 1
            return f"┌{lbl}{'─' * max(gap, 1)}{right_tag}┐"
        return f"┌{lbl}{'─' * (inner - _vlen(lbl))}┐"
    return f"┌{'─' * inner}┐"


def _box_bot(width: int) -> str:
    return f"└{'─' * (width - 2)}┘"


def _box_row(term: blessed.Terminal, content: str, width: int) -> str:
    """│ content (padded) │  — content is already styled."""
    inner = width - 4
    padded = _rpad(content, inner)
    return term.cyan("│") + " " + padded + term.normal + " " + term.cyan("│")


def _box_empty(term: blessed.Terminal, width: int) -> str:
    return term.cyan("│") + " " * (width - 2) + term.cyan("│")


# ── Sparkline ─────────────────────────────────────────────────────────────────

def sparkline(data: List[float], width: int) -> str:
    """Returns a unicode sparkline of `width` characters from the given data."""
    if not data:
        return "▁" * width
    data = data[-width:]
    lo, hi = min(data), max(data)
    rng = hi - lo if hi != lo else 1.0
    return "".join(_SPARK_CHARS[min(int((v - lo) / rng * 7), 7)] for v in data)


# ── Header ────────────────────────────────────────────────────────────────────

def _draw_header(term: blessed.Terminal, current_time: str, max_width: int = 80) -> None:
    """Draws the top header: double outer box, title left, clock right."""
    w = max_width
    title     = "PHEMEX SIM BOT"
    badge     = "◈ PAPER"
    # Raw visible widths for gap calculation
    left_raw  = f"  ⚡ {title}  {badge}"
    right_raw = f"{current_time}  "
    gap       = max(0, w - 2 - len(left_raw) - len(right_raw))

    left_styled  = (
        "  ⚡ "
        + term.bold_cyan(title)
        + "  "
        + term.yellow(badge)
    )
    right_styled = term.bold_white(current_time) + "  "
    body = term.cyan("║") + left_styled + " " * gap + right_styled + term.cyan("║")

    print(term.move_xy(2, 1) + term.cyan("╔" + "═" * (w - 2) + "╗"))
    print(term.move_xy(2, 2) + body)
    print(term.move_xy(2, 3) + term.cyan("╠" + "═" * (w - 2) + "╣"))


# ── Positions ─────────────────────────────────────────────────────────────────

def _draw_positions_section(
    term: blessed.Terminal,
    positions: List[Dict[str, Any]],
    current_prices: Dict[str, float],
    start_row: int,
    max_width: int = 80,
) -> int:
    """Renders the active-positions panel."""
    w   = max_width
    row = start_row
    n   = len(positions)

    slot_tag = f"─ {n} open " if n else "─ idle "
    print(term.move_xy(2, row) + term.cyan(_box_top("OPEN POSITIONS", w, slot_tag)))
    row += 1

    if not positions:
        msg = term.white("  Waiting for qualifying setups") + term.cyan(" ·")
        print(term.move_xy(2, row) + _box_row(term, _rpad(msg, w - 4), w))
        row += 1
    else:
        for pos in positions:
            sym        = pos["symbol"]
            side       = pos["side"]
            entry      = float(pos["entry"])
            size       = float(pos["size"])
            stop       = float(pos.get("stop_price", 0))
            tp         = float(pos.get("take_profit", 0))
            orig_stop  = float(pos.get("original_stop", stop))
            score      = pos.get("entry_score", 0)
            now        = current_prices.get(sym)
            is_long    = side == "Buy"

            upnl = 0.0
            if now:
                upnl = (now - entry) * size if is_long else (entry - now) * size

            # Direction badge
            dir_badge = (
                term.bold_green("▲ LONG ") if is_long else term.bold_red("▼ SHORT")
            )
            now_str = f"{now:.5g}" if now else "·······"
            if now:
                pnl_str = (
                    term.bold_green(f"+{upnl:.4f}")
                    if upnl >= 0 else term.bold_red(f"{upnl:.4f}")
                )
            else:
                pnl_str = term.white("·······")

            margin_s = term.cyan(f"M: ${pos.get('margin', 0.0):.1f}")
            leverage_s = term.white(f"{pos.get('leverage', '??')}x")
            # ── Row 1: direction · symbol · entry → now · pnl · score ──────
            score_badge = term.yellow(f"[{score}]")
            arrow       = term.white("──▶")
            entry_s     = term.white(f"{entry:.5g}")
            now_s       = term.white(now_str)
            sym_s       = term.bold_white(f"{sym:<12}")

            # Duration
            dur_str = "???"
            entry_time_str = pos.get("entry_time")
            if entry_time_str:
                try:
                    entry_dt = datetime.datetime.fromisoformat(entry_time_str)
                    diff = datetime.datetime.now() - entry_dt
                    tot_sec = int(diff.total_seconds())
                    if tot_sec < 60: dur_str = f"{tot_sec}s"
                    elif tot_sec < 3600: dur_str = f"{tot_sec//60}m"
                    else: dur_str = f"{tot_sec//3600}h {(tot_sec%3600)//60}m"
                except Exception:
                    pass
            dur_badge = term.white(f"({dur_str})")

            line1 = f" {dir_badge} {sym_s} {entry_s} {arrow} {now_s}  {margin_s} {leverage_s}  {pnl_str}  {score_badge} {dur_badge}"
            print(term.move_xy(2, row) + _box_row(term, line1, w))
            row += 1

            # ── Row 2: price-position bar ────────────────────────────────────
            if now:
                bar_w = w - 16
                pts   = [orig_stop, stop, entry, now, tp]
                lo    = min(pts)
                hi    = max(pts)
                rng   = (hi - lo) if hi != lo else 1.0

                def gp(v: float) -> int:
                    return max(0, min(bar_w - 1, int((v - lo) / rng * (bar_w - 1))))

                bar = list("─" * bar_w)
                bar[gp(orig_stop)] = term.red("╳")
                bar[gp(stop)]      = term.bold_red("S")
                bar[gp(entry)]     = term.yellow("E")
                bar[gp(tp)]        = term.bold_green("T")
                
                # Replace dot with actual pnl number
                pnl_label = f"{upnl:+.2f}"
                pnl_color = term.bold_green if upnl >= 0 else term.bold_red
                pnl_styled = pnl_color(pnl_label)
                
                # Calculate where to insert the PnL label in the bar
                pos = gp(now)
                # Ensure the label doesn't go out of bounds
                if pos + len(pnl_label) > bar_w:
                    pos = bar_w - len(pnl_label)
                
                # Create a list of styled characters for the bar
                styled_bar_chars = [term.normal("─") for _ in range(bar_w)]
                styled_bar_chars[gp(orig_stop)] = term.red("╳")
                styled_bar_chars[gp(stop)]      = term.bold_red("S")
                styled_bar_chars[gp(entry)]     = term.yellow("E")
                styled_bar_chars[gp(tp)]        = term.bold_green("T")
                
                # Insert the styled PnL string
                for i, char in enumerate(pnl_styled):
                    if pos + i < bar_w:
                        styled_bar_chars[pos + i] = char
                
                bar_combined = "".join(styled_bar_chars)

                sl_s  = term.red(f"{stop:.4g}")
                tp_s  = term.green(f"{tp:.4g}")
                label = f"    ╰ SL {sl_s}  TP {tp_s}  " + term.cyan("[") + bar_combined + term.cyan("]")
                print(term.move_xy(2, row) + _box_row(term, label, w))
                row += 1

    print(term.move_xy(2, row) + term.cyan(_box_bot(w)))
    row += 1
    return row


# ── Account + Session (two columns) ──────────────────────────────────────────

def _draw_account_session_section(
    term: blessed.Terminal,
    balance: float,
    locked_margin: float,
    current_upnl: float,
    equity: float,
    total_trades: int,
    wins: int,
    losses: int,
    win_rate: float,
    total_closed_pnl: float,
    start_row: int,
    max_width: int = 80,
    equity_history: List[float] = None
) -> int:
    """Two-column panel: wallet left, session stats right."""
    
    # Use passed history to avoid global mutation side-effects
    spark_data = equity_history if equity_history else []

    w   = max_width
    lw  = 36          # left column width
    gap = 2
    rw  = w - lw - gap

    eq_delta   = equity - INITIAL_BALANCE
    eq_color   = term.bold_green  if eq_delta  >= 0 else term.bold_red
    upnl_color = term.green       if current_upnl >= 0 else term.red
    rpnl_color = term.bold_green  if total_closed_pnl >= 0 else term.bold_red

    # ── Left panel: wallet ────────────────────────────────────────────────────
    left_lines: List[str] = []
    left_lines.append(term.cyan(_box_top("WALLET", lw)))
    left_lines.append(_box_row(term,
        "  Available" + term.bold_white(f"${balance:9.2f}") + term.cyan(" USDT"), lw))
    left_lines.append(_box_row(term,
        "  Locked   " + term.yellow(f"${locked_margin:9.2f}") + term.cyan(" USDT"), lw))
    left_lines.append(_box_row(term,
        "  uPnL     " + upnl_color(f"{current_upnl:+.4f}") + term.cyan(" USDT"), lw))
    left_lines.append(_box_row(term,
        "  Equity   " + eq_color(f"${equity:9.2f}") + term.cyan(" USDT"), lw))
    left_lines.append(term.cyan(_box_bot(lw)))

    # ── Right panel: statistics ───────────────────────────────────────────────
    right_lines: List[str] = []
    right_lines.append(term.cyan(_box_top("STATISTICS", rw)))
    right_lines.append(_box_row(term,
        "  Trades  " + term.bold_white(str(total_trades).ljust(4)), rw))
    right_lines.append(_box_row(term,
        f"  {term.bold_green(f'✅ {wins}W')}   {term.bold_red(f'❌ {losses}L')}"
        f"   Rate {term.yellow(f'{win_rate:.1f}%')}", rw))
    right_lines.append(_box_row(term,
        "  Realized  " + rpnl_color(f"{total_closed_pnl:+.4f}") + term.cyan(" USDT"), rw))
    right_lines.append(_box_empty(term, rw))
    right_lines.append(term.cyan(_box_bot(rw)))

    row = start_row
    for l_line, r_line in zip(left_lines, right_lines):
        print(term.move_xy(2, row) + l_line + " " * gap + r_line)
        row += 1

    return row


# ── Trade history ─────────────────────────────────────────────────────────────

def _draw_history_section(
    term: blessed.Terminal,
    history: List[Dict[str, Any]],
    start_row: int,
    max_width: int = 80,
) -> int:
    """Two-per-row closed trade history (last 40)."""
    w      = max_width
    row    = start_row
    recent = history[::-1][:40]

    print(term.move_xy(2, row) + term.cyan(_box_top("TRADE HISTORY", w)))
    row += 1

    if not recent:
        msg = term.white("  No closed trades yet")
        print(term.move_xy(2, row) + _box_row(term, msg, w))
        row += 1
    else:
        col_w = (w - 6) // 2  # visible width for one trade cell

        def _fmt(t: dict) -> str:
            pnl   = t["pnl"]
            c     = term.bold_green if pnl > 0 else term.bold_red
            badge = "✅" if pnl > 0 else "❌"
            ts    = t["timestamp"][11:16]
            sym   = t["symbol"][:10].ljust(10)
            d     = t["direction"][:5].ljust(5)
            return f" {term.white(ts)} {term.bold_white(sym)} {term.cyan(d)} {badge} {c(f'{pnl:+.4f}')}"

        for i in range(0, len(recent), 2):
            left_cell = _fmt(recent[i])
            if i + 1 < len(recent):
                right_cell = _fmt(recent[i + 1])
                sep        = term.cyan("│")
                content    = _rpad(left_cell, col_w) + sep + right_cell
            else:
                content = left_cell
            print(term.move_xy(2, row) + _box_row(term, content, w))
            row += 1

    print(term.move_xy(2, row) + term.cyan(_box_bot(w)))
    row += 1
    return row

def _draw_equity_chart_section(
    term: blessed.Terminal,
    equity_history: List[float],
    start_row: int,
    max_width: int = 120,
) -> int:
    """Full-width block-bar equity performance chart."""
    w   = max_width
    row = start_row
    h   = 3  # Taller/thicker (3 rows of blocks)
    
    inner_w = w - 8
    if not equity_history:
        chart_lines = [" " * inner_w] * h
    else:
        # Prepare data
        data = equity_history[-inner_w:]
        while len(data) < inner_w:
            data = ([data[0]] if data else [0.0]) + data
        
        lo, hi = min(data), max(data)
        rng = hi - lo if hi != lo else 1.0
        
        chart_lines = []
        # We'll use the _SPARK_CHARS: "▁▂▃▄▅▆▇█"
        # Since it has 8 chars, each row has 8 levels of granularity.
        for r in range(h - 1, -1, -1):
            line = ""
            for v in data:
                # Scale v to 0 .. (h * 8 - 1)
                scaled = int((v - lo) / rng * (h * 8 - 1))
                # Determine how many 'units' are in THIS specific row
                row_units = scaled - (r * 8)
                
                if row_units >= 7:
                    line += "█"
                elif row_units < 0:
                    line += " "
                else:
                    # Map 0..6 to the spark chars (avoiding the full block █ which is row_units >= 7)
                    line += _SPARK_CHARS[max(0, row_units)]
            chart_lines.append(line)

    print(term.move_xy(2, row) + term.cyan(_box_top("EQUITY PERFORMANCE", w)))
    row += 1
    
    # Determine color based on trend
    color = term.green if (equity_history and equity_history[-1] >= INITIAL_BALANCE) else term.red
    
    for line in chart_lines:
        print(term.move_xy(2, row) + _box_row(term, color(line), w))
        row += 1
        
    print(term.move_xy(2, row) + term.cyan(_box_bot(w)))
    row += 1
    return row

def _draw_consolidated_positions(
    term: blessed.Terminal,
    positions: List[Dict[str, Any]],
    current_prices: Dict[str, float],
    max_width: int = 120,
) -> None:
    """
    Renders open positions at the bottom of the screen with consolidated stats
    and a braille PnL chart inside the box.
    """
    if not positions:
        return

    # Determine start row (bottom-anchored)
    # Each position box is now ~10 rows high with compact chart.
    display_positions = positions[-5:]
    chart_h = 4
    box_h = chart_h + 6 
    
    # Start drawing from roughly the bottom
    start_y = term.height - (len(display_positions) * (box_h + 1)) - 2
    
    for idx, pos in enumerate(display_positions):
        row = start_y + (idx * (box_h + 1))
        sym        = pos["symbol"]
        side       = pos["side"]
        entry      = float(pos["entry"])
        size       = float(pos["size"])
        stop       = float(pos.get("stop_price", 0))
        tp         = float(pos.get("take_profit", 0))
        margin     = float(pos.get("margin", 0))
        now        = current_prices.get(sym)
        is_long    = side == "Buy"
        
        hist = _pnl_histories.get(sym, [0.0])
        upnl = 0.0
        if now:
            upnl = (now - entry) * size if is_long else (entry - now) * size

        # --- Header & Stats line ---
        dir_badge = term.bold_green("▲ LONG") if is_long else term.bold_red("▼ SHORT")
        pnl_color = term.bold_green if upnl >= 0 else term.bold_red
        
        # Duration
        dur_str = "???"
        entry_time_str = pos.get("entry_time")
        if entry_time_str:
            try:
                entry_dt = datetime.datetime.fromisoformat(entry_time_str)
                diff = datetime.datetime.now() - entry_dt
                tot_sec = int(diff.total_seconds())
                if tot_sec < 60: dur_str = f"{tot_sec}s"
                elif tot_sec < 3600: dur_str = f"{tot_sec//60}m"
                else: dur_str = f"{tot_sec//3600}h {(tot_sec%3600)//60}m"
            except Exception:
                pass
        dur_badge = term.white(f"({dur_str})")

        header = f" {dir_badge} {term.bold_white(sym)}  Entry: {term.white(f'{entry:.5g}')}  Now: {term.white(f'{now:.5g}' if now else '...')}  {dur_badge}"
        stats  = f"    Margin: {term.yellow(f'${margin:.2f}')}  PnL: {pnl_color(f'{upnl:+.4f}')} USDT"
        
        print(term.move_xy(2, row) + term.cyan(_box_top("", max_width)))
        print(term.move_xy(4, row) + header)
        row += 1
        print(term.move_xy(2, row) + _box_row(term, stats, max_width))
        row += 1

        # --- Braille Chart Area ---
        chart_w = max_width - 20
        # Use existing chart logic but capture lines
        chart_lines = render_pnl_chart(
            pnl_history=hist,
            width=chart_w,
            height=chart_h,
            label="", # label already in header
            term=None # get strings back
        )
        
        # Strip the first (label) and last two (axis/labels) lines from render_pnl_chart output
        # since we want to custom integrate them
        core_chart = chart_lines[1:-2]
        
        for i, line in enumerate(core_chart):
            # If it's the top line of the chart, add "EXITS" indicator
            if i == 0:
                line = line + term.bold_red("  ← EXITS")
            print(term.move_xy(2, row + i) + term.cyan("│ ") + line + term.move_xy(max_width-1, row+i) + term.cyan("│"))
        
        row += len(core_chart)

        # --- Price Line (Entry/Stop/TP/Now) ---
        bar_w = max_width - 16
        pts   = [stop, entry, tp]
        if now: pts.append(now)
        lo    = min(pts)
        hi    = max(pts)
        rng   = (hi - lo) if hi != lo else 1.0
        def gp(v: float) -> int:
            return max(0, min(bar_w - 1, int((v - lo) / rng * (bar_w - 1))))

        bar = list("─" * bar_w)
        bar[gp(stop)]  = term.red("S")
        bar[gp(entry)] = term.yellow("E")
        bar[gp(tp)]    = term.green("T")
        # Replace dot with actual pnl number (current price indicator)
        if now is not None:
            pnl_label = f"{upnl:+.2f}"
            pnl_color = term.bold_green if upnl >= 0 else term.bold_red
            pnl_styled = pnl_color(pnl_label)
            
            # Calculate where to insert the PnL label in the bar
            pos = gp(now)
            # Ensure the label doesn't go out of bounds
            if pos + len(pnl_label) > bar_w:
                pos = bar_w - len(pnl_label)
            
            # Insert the styled PnL string
            # We put the whole styled string into one slot and clear the others it covers visually
            bar[pos] = pnl_styled
            for i in range(1, len(pnl_label)):
                if pos + i < bar_w:
                    bar[pos + i] = ""
        
        price_line = term.cyan("[") + "".join(bar) + term.cyan("]")
        print(term.move_xy(2, row) + _box_row(term, f"  Price: {price_line}", max_width))
        row += 1
        
        print(term.move_xy(2, row) + term.cyan(_box_bot(max_width)))

# ── System log ────────────────────────────────────────────────────────────────

def _draw_system_logs_section(
    term: blessed.Terminal,
    logs: List[str],
    start_row: int,
    max_width: int = 80,
) -> int:
    """Color-coded scrolling log panel (last 6 entries)."""
    w   = max_width
    row = start_row

    print(term.move_xy(2, row) + term.cyan(_box_top("SYSTEM LOG", w)))
    row += 1

    with _log_lock:
        # deques don't support slicing, convert to list first
        display_logs = list(logs)[-6:]

    # Always render exactly 6 rows
    while len(display_logs) < 6:
        display_logs.append("")

    for entry in display_logs:
        if not entry:
            print(term.move_xy(2, row) + _box_empty(term, w))
        else:
            # Logs are already formatted with ANSI colors by setup_colored_logging
            print(term.move_xy(2, row) + _box_row(term, entry, w))

        row += 1

    print(term.move_xy(2, row) + term.cyan(_box_bot(w)))
    row += 1
    return row


# ── Footer ────────────────────────────────────────────────────────────────────

def _draw_footer(term: blessed.Terminal, row: int, max_width: int = 80) -> None:
    """Bottom bar with keyboard shortcuts."""
    w          = max_width
    left_raw   = "  [S] Close All  [Q] Quit  "
    right_raw  = "  ⚡ FANCYBOT v2  "
    gap        = max(0, w - 2 - len(left_raw) - len(right_raw))

    left_part  = (
        "  "
        + term.bold_white("[S]") + term.white(" Close All")
        + "  "
        + term.bold_white("[Q]") + term.white(" Quit")
        + "  "
    )
    right_part = "  ⚡ " + term.bold_cyan("FANCYBOT") + term.white(" v2") + "  "
    inner      = left_part + term.cyan("─" * gap) + right_part
    line       = term.cyan("╚═") + inner + term.normal + term.cyan("═╝")
    print(term.move_xy(2, row) + line)


# ─────────────────────────────────────────────────────────────────────────────
# TUI — Main Display Loop
# ─────────────────────────────────────────────────────────────────────────────

def _live_pnl_display() -> None:
    """Full-screen TUI dashboard — runs in a dedicated daemon thread."""
    global _display_thread_running, _equity_history
    term         = blessed.Terminal()
    results_file = SCRIPT_DIR / "sim_trade_results.json"

    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        try:
            while True:
                if _display_paused.is_set():
                    time.sleep(0.5)
                    continue

                acc       = load_paper_account()
                positions = acc.get("positions", [])

                history: List[dict] = []
                if results_file.exists():
                    try:
                        history = json.loads(results_file.read_text())
                    except Exception:
                        pass

                wins             = [t for t in history if t["pnl"] > 0]
                losses           = [t for t in history if t["pnl"] <= 0]
                total_trades     = len(history)
                win_rate         = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
                total_closed_pnl = sum(t["pnl"] for t in history)
                current_time     = datetime.datetime.now().strftime("%H:%M:%S")
                balance          = acc.get("balance", 0.0)

                with _prices_lock:
                    current_upnl = 0.0
                    locked_margin = 0.0
                    for p in positions:
                        locked_margin += p.get("margin", 0.0)
                        if _live_prices.get(p["symbol"]):
                            now = _live_prices[p["symbol"]]
                            entry = p["entry"]
                            size = float(p["size"])
                            pos_pnl = (now - entry) * size if p["side"] == "Buy" else (entry - now) * size
                            current_upnl += pos_pnl
                            update_pnl_history(p["symbol"], pos_pnl)
                
                equity = balance + locked_margin + current_upnl
                
                # Update equity history here (state mutation), strictly outside render function
                _equity_history.append(equity)
                if len(_equity_history) > _max_history:
                    _equity_history.pop(0)

                max_w = 120
                print(term.clear)
                _draw_header(term, current_time, max_w)
                
                row = 4
                # Top sections (Account, History, Logs)
                row = _draw_account_session_section(
                    term, balance, locked_margin, current_upnl, equity,
                    total_trades, len(wins), len(losses),
                    win_rate, total_closed_pnl, row, max_w,
                    _equity_history
                )
                row = _draw_history_section(term, history, row, max_w)
                row = _draw_equity_chart_section(term, _equity_history, row, max_w)
                row = _draw_system_logs_section(term, _bot_logs, row, max_w)
                
                # Consolidated Positions at the bottom
                _draw_consolidated_positions(term, positions, _live_prices, max_w)
                
                # Footer fixed at the very bottom line
                _draw_footer(term, term.height - 1, max_w)

                key = term.inkey(timeout=0.8)
                if key.lower() == "s":
                    _display_paused.set()
                    confirm_row = row + 1
                    print(
                        term.move_xy(4, confirm_row)
                        + term.on_red(term.bold_white("  ⚠  CLOSE ALL TRADES?  "))
                        + term.bold_yellow("  (Y / N)  "),
                        end="", flush=True,
                    )
                    if term.inkey().lower() == "y":
                        _close_all_positions()
                        time.sleep(1)
                    _display_paused.clear()
                elif key.lower() == "q":
                    break

        except KeyboardInterrupt:
            pass
        finally:
            with _display_lock:
                _display_thread_running = False


# ─────────────────────────────────────────────────────────────────────────────
# Simulation Overrides
# ─────────────────────────────────────────────────────────────────────────────

def get_sim_balance() -> float:
    """Returns the current wallet balance from the paper account."""
    return load_paper_account().get("balance", 0.0)


def get_sim_positions() -> List[dict]:
    """Returns the list of open paper positions."""
    return load_paper_account().get("positions", [])


def check_opposite_signal(symbol: str, side: str, ticker: Optional[dict] = None) -> Tuple[bool, int]:
    """
    Scans the opposite direction for a symbol to see if a reversal is building.
    Returns (True, score) if score >= EXIT_SIGNAL_SCORE_THRESHOLD.
    """
    global LAST_EXIT_SCAN_TIME
    now = time.time()
    if now - LAST_EXIT_SCAN_TIME.get(symbol, 0) < EXIT_SIGNAL_SCAN_INTERVAL:
        return False, 0
    
    LAST_EXIT_SCAN_TIME[symbol] = now
    
    try:
        # Fetch fresh ticker if not provided
        if not ticker:
            tickers = pc.get_tickers()
            ticker = next((t for t in tickers if t["symbol"] == symbol), None)
            
        if not ticker:
            return False, 0
            
        # Use the opposite scanner module
        scanner = scanner_short if side == "Buy" else scanner_long
        
        # Minimal config for quick scan - use 15m to catch reversals faster
        cfg = {
            "TIMEFRAME": "15m",
            "MIN_VOLUME": 0,
            "RATE_LIMIT_RPS": 100.0,
            "CANDLES": 100
        }
        
        res = scanner.analyse(ticker, cfg, enable_ai=False, enable_entity=False)
        if res and res["score"] >= EXIT_SIGNAL_SCORE_THRESHOLD:
            return True, res["score"]
            
    except Exception as e:
        logger.debug(f"Error in check_opposite_signal for {symbol}: {e}")
        
    return False, 0


def update_pnl_and_stops() -> None:
    """
    Polls live prices for all open positions, updates PnL, and evaluates
    trailing-stop and take-profit levels.
    """
    # Initialize outside lock to avoid UnboundLocalError
    ticker_map: Dict[str, Any] = {}
    missing: List[str] = []

    # Narrow lock scope — only hold lock while reading/writing the account structure.
    # Move all I/O (Telegram, Logging) outside the lock.
    with _stop_lock:
        acc = load_paper_account()
        if not acc["positions"]:
            return

        # Fetch REST tickers only for symbols not yet in the live-price cache
        missing = [p["symbol"] for p in acc["positions"] if p["symbol"] not in _live_prices]
        if missing:
            # Releasing stop lock during ticker fetch to avoid blocking stop evaluations
            pass
    
    # Fetch tickers for all open positions to provide fresh data for opposite signal checks
    try:
        tickers    = pc.get_tickers()
        ticker_map = {t["symbol"]: t for t in tickers}
    except Exception as e:
        logger.debug(f"Failed to fetch REST tickers in update loop: {e}")

    # To store events for I/O outside the lock
    exits_to_process = []

    with _stop_lock:
        # Reload account in case it changed during the REST fetch
        acc = load_paper_account()
        
        # Re-derive current positions state to avoid phantom PnL on just-closed symbols
        # (Though update_pnl_and_stops iterates over acc["positions"], 
        # using ticker_map from a previous stale missing list is the risk)
        
        new_positions: List[dict] = []
        closed_any = False

        for pos in acc["positions"]:
            symbol = pos["symbol"]
            with _prices_lock:
                current_price = _live_prices.get(symbol)

            if current_price is None:
                ticker = ticker_map.get(symbol)
                if ticker:
                    current_price = float(ticker.get("lastRp") or 0.0)

            if not current_price:
                new_positions.append(pos)
                continue

            side = pos["side"]
            exit_reason = None
            exit_price  = 0.0
            pnl         = 0.0
            
            # Use .get() for stop_price and check existence
            stop_price = pos.get("stop_price")
            if stop_price is None:
                new_positions.append(pos)
                continue

            if side == "Buy":
                if current_price > pos.get("high_water", 0.0):
                    pos["high_water"] = current_price
                    pos["stop_price"] = current_price * (1.0 - p_bot.TRAIL_PCT)

                if current_price <= stop_price:
                    exit_reason, exit_price = "Stop Loss", stop_price
                elif "take_profit" in pos and current_price >= pos["take_profit"]:
                    exit_reason, exit_price = "Take Profit", pos["take_profit"]
                
                if exit_reason:
                    pnl = (exit_price - pos["entry"]) * pos["size"]
            else:
                if current_price < pos.get("low_water", 999_999_999.0):
                    pos["low_water"]  = current_price
                    pos["stop_price"] = current_price * (1.0 + p_bot.TRAIL_PCT)

                if current_price >= stop_price:
                    exit_reason, exit_price = "Stop Loss", stop_price
                elif "take_profit" in pos and current_price <= pos["take_profit"]:
                    exit_reason, exit_price = "Take Profit", pos["take_profit"]
                
                if exit_reason:
                    pnl = (pos["entry"] - exit_price) * pos["size"]

            # ── Check for opposite signal exit ────────────────────────
            if not exit_reason:
                ticker = ticker_map.get(symbol)
                opp_hit, opp_score = check_opposite_signal(symbol, side, ticker=ticker)
                if opp_hit:
                    exit_reason, exit_price = f"Reversal (Score {opp_score})", current_price
                    pnl = (exit_price - pos["entry"]) * pos["size"] if side == "Buy" else (pos["entry"] - exit_price) * pos["size"]

            if exit_reason:
                # Store the exit data for processing after the lock is released
                exits_to_process.append({
                    "symbol": symbol,
                    "side": side,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "margin": pos.get("margin", 0.0),
                    "entry": pos["entry"],
                    "size": pos["size"],
                    "entry_score": pos.get("entry_score", 0),
                    "entry_time": pos.get("entry_time"),
                })
                acc["balance"] += (pos.get("margin", 0.0) + pnl)
                closed_any = True
                continue

            # Position remains open
            pos["pnl"] = (current_price - pos["entry"]) * pos["size"] if side == "Buy" else (pos["entry"] - current_price) * pos["size"]
            new_positions.append(pos)

        if closed_any:
            acc["positions"] = new_positions
            save_paper_account(acc)

    # Process I/O (exits) outside the lock to avoid blocking
    for ex in exits_to_process:
        symbol = ex["symbol"]
        with _cooldown_lock:
            _stop_contexts[symbol] = capture_stop_context(
                symbol, ex["exit_price"],
                is_stop_loss=("Stop" in ex["exit_reason"]),
                direction="LONG" if ex["side"] == "Buy" else "SHORT"
            )
        save_sim_cooldowns()
        _slot_available_event.set()

        tui_log(f"{ex['exit_reason'].upper()} HIT: {symbol} closed at {ex['exit_price']}")
        pnl_emoji = "✅" if ex['pnl'] > 0 else "❌"
        # Duration
        hold_time = 0
        if ex.get("entry_time"):
            try:
                hold_time = (datetime.datetime.now() - datetime.datetime.fromisoformat(ex["entry_time"])).total_seconds()
            except Exception:
                pass
        h_min, h_sec = divmod(int(hold_time), 60)
        h_hour, h_min = divmod(h_min, 60)
        dur_str = f"{h_hour}h {h_min}m" if h_hour > 0 else (f"{h_min}m {h_sec}s" if h_min > 0 else f"{h_sec}s")

        send_telegram_message(
            f"🔔 *SIM TRADE CLOSED ({ex['exit_reason']})*\n\n"
            f"*Symbol:* {symbol}\n*Side:* {ex['side']}\n"
            f"*Exit Price:* {ex['exit_price']}\n"
            f"*PnL:* {pnl_emoji} {ex['pnl']:+.4f} USDT\n"
            f"*Duration:* {dur_str}\n"
            f"*Time:* {datetime.datetime.now().strftime('%H:%M:%S')}"
        )
        _log_closed_trade(
            symbol, ex['side'], ex['entry'], ex['exit_price'], ex['size'],
            ex['entry_score'], ex['entry_time'],
            "stop" if "Stop" in ex['exit_reason'] else "tp"
        )


def verify_sim_candidate(symbol: str, direction: str, original_score: int, wait_seconds: int = 20) -> Optional[dict]:
    """
    Waits, then re-scans a single symbol to verify the signal is still valid for simulation.
    Performs iterative checks to ensure price action isn't moving against the signal.
    """
    steps = 3
    step_wait = wait_seconds / steps
    initial_price = None
    last_result = None
    
    tui_log(f"VERIFY: {symbol} ({direction}) for {wait_seconds}s...")
    
    for i in range(steps):
        time.sleep(step_wait)
        
        # Fetch fresh ticker
        try:
            tickers = pc.get_tickers()
            ticker = next((t for t in tickers if t["symbol"] == symbol), None)
        except Exception as e:
            tui_log(f"FAIL: Error fetching ticker for {symbol}: {e}")
            return None

        if not ticker:
            tui_log(f"FAIL: {symbol} ticker not found during verification.")
            return None

        current_price = float(ticker.get("lastRp") or ticker.get("closeRp") or 0.0)
        if initial_price is None:
            initial_price = current_price
            
        # Price movement check
        price_change = pc.pct_change(current_price, initial_price)
        
        if direction == "LONG":
            if price_change < -0.6: # Dropping too much during verification
                tui_log(f"FAIL: {symbol} dropping during verify: {price_change:+.2f}%")
                return None
        else: # SHORT
            if price_change > 0.6: # Pumping too much during verification
                tui_log(f"FAIL: {symbol} pumping during verify: {price_change:+.2f}%")
                return None

        # Re-scan using the appropriate scanner module
        scanner = scanner_long if direction == "LONG" else scanner_short
        
        # Minimal config for re-scan (using p_bot's constants)
        cfg = {
            "TIMEFRAME": p_bot.TIMEFRAME,
            "MIN_VOLUME": p_bot.MIN_VOLUME,
            "RATE_LIMIT_RPS": p_bot.RATE_LIMIT_RPS,
            "CANDLES": 100
        }
        
        fresh_result = scanner.analyse(ticker, cfg, enable_ai=False, enable_entity=False)
        
        if not fresh_result:
            tui_log(f"FAIL: {symbol} no longer qualifies at step {i+1}")
            return None
            
        fresh_score = fresh_result["score"]
        
        # Spread check: avoid illiquid assets that may have fake signals
        current_spread = fresh_result.get("spread", 0.0)
        if current_spread is not None and current_spread > 0.25:
            tui_log(f"FAIL: {symbol} spread too high: {current_spread:.2f}%")
            return None
            
        # RSI Momentum Check: Ensure RSI isn't deep in the "over-exhaustion" zone already
        current_rsi = fresh_result.get("rsi")
        if current_rsi:
            if direction == "LONG" and current_rsi > 70:
                tui_log(f"FAIL: {symbol} RSI {current_rsi:.1f} — overbought after wait.")
                return None
            elif direction == "SHORT" and current_rsi < 30:
                tui_log(f"FAIL: {symbol} RSI {current_rsi:.1f} — oversold after wait.")
                return None
            
        # Allow 15% score degradation during the iterative check
        if fresh_score < original_score * 0.85:
            tui_log(f"FAIL: {symbol} score dropped: {original_score} -> {fresh_score}")
            return None
            
        last_result = fresh_result
        tui_log(f"  Step {i+1}/{steps}: {symbol} score {fresh_score} ({price_change:+.2f}%)")

    # Final overextension check - avoid chasing if it moved too far in our direction too fast
    final_change = pc.pct_change(last_result["price"], initial_price)
    if abs(final_change) > 1.5:
        tui_log(f"FAIL: {symbol} overextended ({final_change:+.2f}%) during verify.")
        return None

    tui_log(f"VERIFIED: {symbol} score {last_result['score']} — ready for SIM entry.")
    return last_result

def get_readiness_for_symbol(symbol: str, stop_ctx: pc.StopContext, result: dict, direction: str = "SHORT") -> Tuple[float, float]:
    """
    Calculates Readiness Score R and position size scalar.
    Returns (R, scalar).
    """
    # 1. ATR
    # Unified analyse returns atr_stop_pct = (0.5 * atr / last * 100.0)
    # So atr = atr_stop_pct * last / 100.0 / 0.5
    last_price = result["price"]
    atr_stop_pct = result.get("atr_stop_pct", 0.0)
    current_atr = (atr_stop_pct * last_price / 50.0) if atr_stop_pct > 0 else 0.0
    
    if current_atr == 0:
        # Fallback fetch
        candles_raw = pc.get_candles(symbol, timeframe=p_bot.TIMEFRAME, limit=100)
        closes = [float(c[6]) for c in candles_raw]
        highs  = [float(c[4]) for c in candles_raw]
        lows   = [float(c[5]) for c in candles_raw]
        current_atr = pc.calc_atr(highs, lows, closes) or 0.0
    
    # 2. Spread
    current_spread = result.get("spread", 0.05)
    
    # 3. Order Book Volumes
    try:
        _, _, _, bid_vol, ask_vol = pc.get_order_book(symbol)
    except Exception:
        bid_vol, ask_vol = 1.0, 1.0 # fallback neutral
    
    # 4. Candles for Price Behaviour
    candles_raw = pc.get_candles(symbol, timeframe=p_bot.TIMEFRAME, limit=24)
    candles = [(float(c[3]), float(c[4]), float(c[5]), float(c[6])) for c in candles_raw]
    
    r = pc.calc_readiness_score(
        symbol=symbol,
        stop_ctx=stop_ctx,
        current_atr=current_atr,
        current_spread=current_spread,
        bid_vol=bid_vol,
        ask_vol=ask_vol,
        candles=candles,
        buy_vol=None, # redistributed in common
        sell_vol=None,
        intended_direction=direction
    )
    
    scalar = pc.get_readiness_scalar(r)
    return r, scalar

def execute_sim_setup(result: dict, direction: str) -> bool:
    """
    Opens a new simulated position from a scanner result.
    Returns True on success, False if the trade is skipped.
    """
    symbol = result["inst_id"]
    price  = result["price"]
    score  = result["score"]

    with _stop_lock:
        acc = load_paper_account()

        if any(p["symbol"] == symbol for p in acc["positions"]):
            return False

        # --- Dynamic Cooldown Check ---
        scalar = 1.0
        stop_ctx = None
        with _cooldown_lock:
            stop_ctx = _stop_contexts.get(symbol)
            
        if stop_ctx:
            r, scalar = get_readiness_for_symbol(symbol, stop_ctx, result, direction=direction)
            if r < 0.35:
                tui_log(f"COOLDOWN: {symbol} — Readiness R={r:.2f} < 0.35 (Blocked)", event_type="SKIP")
                return False
            
            with _cooldown_lock:
                if r >= 0.95:
                    tui_log(f"COOLDOWN: {symbol} — Readiness R={r:.2f} ≥ 0.95 (Cleared!)", event_type="COOLDOWN")
                    if symbol in _stop_contexts:
                        del _stop_contexts[symbol]
                    save_sim_cooldowns()
                else:
                    tui_log(f"COOLDOWN: {symbol} — Readiness R={r:.2f} (Scalar: {scalar:.2f}x)", event_type="COOLDOWN")

        signals       = result.get("signals", [])
        is_low_liq    = any("Low Liquidity" in s for s in signals)
        is_htf        = any("HTF Alignment" in s for s in signals)

        # Tiered score gate
        effective_min = (
            p_bot.MIN_SCORE_LOW_LIQ    if is_low_liq else
            p_bot.MIN_SCORE_HTF_BYPASS if is_htf     else
            p_bot.MIN_SCORE_DEFAULT
        )
        if score < effective_min:
            tui_log(f"SKIP: {symbol} score {score} < effective min {effective_min}")
            return False

        # Parameters and Margin calculation
        if is_low_liq:
            active_leverage  = p_bot.LOW_LIQ_LEVERAGE
            active_trail_pct = p_bot.LOW_LIQ_TRAIL_PCT
            margin_to_use    = p_bot.LOW_LIQ_MARGIN
            tui_log(f"{symbol}: LOW-LIQ MODE — {active_leverage}x lev, "
                    f"{active_trail_pct*100:.1f}% trail, ${margin_to_use} margin")
        else:
            active_leverage  = p_bot.LEVERAGE
            active_trail_pct = p_bot.TRAIL_PCT
            
            # --- Kelly Position Sizing (Standard Trades) ---
            total_trades = _rolling_stats["wins"] + _rolling_stats["losses"]
            
            # Hard ceiling: available balance divided by total allowed slots
            max_per_trade = acc["balance"] / p_bot.MAX_POSITIONS

            if total_trades < 10:
                # Not enough history — just use the max per trade
                margin_to_use = round(max_per_trade, 2)
            else:
                # Transitions to Kelly sizing after 10 trades
                win_rate = _rolling_stats["wins"] / total_trades if total_trades > 0 else 0.5
                avg_win  = _rolling_stats["win_pnl"] / _rolling_stats["wins"] if _rolling_stats["wins"] > 0 else 1.0
                avg_loss = abs(_rolling_stats["loss_pnl"] / _rolling_stats["losses"]) if _rolling_stats["losses"] > 0 else 1.0

                kelly_margin = pc.calc_kelly_margin(
                    bankroll=acc["balance"],
                    win_rate=win_rate,
                    avg_win=avg_win,
                    avg_loss=avg_loss,
                    fraction=0.5
                )
                margin_to_use = round(min(kelly_margin, max_per_trade), 2)
        
        # Apply Readiness Scalar from Dynamic Cooldown
        if scalar < 1.0:
            margin_to_use = round(margin_to_use * scalar, 2)
            tui_log(f"{symbol}: Scaling margin ${margin_to_use} (Scalar: {scalar:.2f}x)")

        if margin_to_use <= 0 or acc["balance"] < margin_to_use:
            tui_log(f"MARGIN FAIL: ${margin_to_use} calculated, but balance ${acc['balance']:.2f} is insufficient.")
            return False

        notional = margin_to_use * active_leverage
        size     = notional / price

        # Deduct margin AND mock taker fee (0.1%)
        fee = notional * 0.001
        acc["balance"] -= (margin_to_use + fee)

        side = "Buy" if direction == "LONG" else "Sell"

        if direction == "LONG":
            stop_px    = price * (1.0 - active_trail_pct)
            tp_px      = price * (1.0 + p_bot.TAKE_PROFIT_PCT)
            high_water = price
            low_water  = None
        else:
            stop_px    = price * (1.0 + active_trail_pct)
            tp_px      = price * (1.0 - p_bot.TAKE_PROFIT_PCT)
            high_water = None
            low_water  = price

        new_pos = {
            "symbol":        symbol,
            "side":          side,
            "size":          size,
            "margin":        margin_to_use,
            "fee":           fee,
            "entry":         price,
            "pnl":           0.0,
            "stop_price":    stop_px,
            "original_stop": stop_px,
            "take_profit":   tp_px,
            "high_water":    high_water,
            "low_water":     low_water,
            "timestamp":     datetime.datetime.now().isoformat(),
            "entry_time":    datetime.datetime.now().isoformat(),
            "entry_score":   score,
        }

        acc["positions"].append(new_pos)
        save_paper_account(acc)

    arrow = "▲ LONG" if direction == "LONG" else "▼ SHORT"
    tui_log(f"ENTERED {arrow} {symbol} @ {price} (Score: {score})")

    # --- Entry Cinematic ---
    if direction == "LONG":
        play_animation(animations.long)
    else:
        play_animation(animations.short)

    emoji = "🚀" if direction == "LONG" else "📉"
    send_telegram_message(
        f"{emoji} *SIM TRADE OPENED*\n\n"
        f"*Symbol:* {symbol}\n"
        f"*Direction:* {direction}\n"
        f"*Price:* {price}\n"
        f"*Score:* {score}\n"
        f"*Time:* {datetime.datetime.now().strftime('%H:%M:%S')}"
    )

    p_bot.log_trade({
        "timestamp": new_pos["timestamp"],
        "symbol":    symbol,
        "direction": direction,
        "price":     price,
        "qty":       str(size),
        "score":     score,
        "status":    "simulated_entry",
    })

    _subscribe_symbol(symbol)
    _ensure_ws_started()

    with _display_lock:
        global _display_thread_running
        if not _display_thread_running:
            _display_thread_running = True
            threading.Thread(target=_live_pnl_display, daemon=True).start()

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main Bot Loop
# ─────────────────────────────────────────────────────────────────────────────

# Hoist helper functions out of the loop
def is_fresh(r: dict, now_dt: datetime.datetime) -> bool:
    ts_raw = r.get("scan_timestamp")
    if not ts_raw:
        return True
    try:
        # Handle string or datetime
        ts = datetime.datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
        return (now_dt - ts).total_seconds() < RESULT_STALENESS_SECONDS
    except (ValueError, TypeError):
        return True

# ── Cluster & Entropy Tracking (Idea 2 & 3) ─────────────────────────
_hawkes_long = pc.HawkesTracker(mu=0.1, alpha=0.8, beta=0.1)
_hawkes_short = pc.HawkesTracker(mu=0.1, alpha=0.8, beta=0.1)
_entropy_penalty = 0

def _get_cluster_threshold_penalty(intensity: float) -> int:
    """Returns a score penalty based on Hawkes intensity (λ)."""
    if intensity > 3.0: return 50  # Major cluster
    if intensity > 2.0: return 30
    if intensity > 1.2: return 15
    return 0

def on_scan_result(r: dict, direction: str) -> None:
    result_time_raw = r.get("scan_timestamp")
    if result_time_raw:
        try:
            # Parse ISO string back to datetime for comparison
            if isinstance(result_time_raw, str):
                result_time = datetime.datetime.fromisoformat(result_time_raw)
            else:
                result_time = result_time_raw
                
            if (datetime.datetime.now() - result_time).total_seconds() > RESULT_STALENESS_SECONDS:
                return
        except (ValueError, TypeError):
            pass

    # ── Hawkes Cluster Throttling (Idea 3) ────────────────────
    tracker = _hawkes_long if direction == "LONG" else _hawkes_short
    intensity = tracker.update(event_occurred=False) # Decay only
    hawkes_penalty = _get_cluster_threshold_penalty(intensity)
    
    # Use global _entropy_penalty from last scan to block cascades
    effective_fast_track = FAST_TRACK_SCORE + hawkes_penalty + _entropy_penalty
    if r["score"] < effective_fast_track:
        if (hawkes_penalty > 0 or _entropy_penalty > 0) and r["score"] >= FAST_TRACK_SCORE:
            tui_log(f"FT THROTTLE: {r['inst_id']} score {r['score']} < dynamic FT threshold {effective_fast_track} (λ={intensity:.2f}, H_pen={_entropy_penalty})")
        return

    # Signal passed the throttle, now we pulse the tracker to reflect this event
    intensity = tracker.update(event_occurred=True)
    hawkes_penalty = _get_cluster_threshold_penalty(intensity) # Update penalty for subsequent logic

    # Move position count and balance check inside _fast_track_lock for atomicity
    with _fast_track_lock:
        current_positions = get_sim_positions()
        
        # Balance check: approximate margin needed
        acc = load_paper_account()
        acc_balance = acc.get("balance", 0.0)
        dynamic_max = p_bot.get_dynamic_max_positions(acc_balance)

        # Account for positions already open PLUS those currently being verified
        if len(current_positions) + len(_fast_track_opened) >= dynamic_max:
            return
        
        # Each pending fast-track will eventually deduct margin + fee
        # (Assuming standard margin for now, verify_sim_candidate will do exact check later)
        pending_margin = len(_fast_track_opened) * (p_bot.MARGIN_USDT * 1.05) # +5% for fees/buffer
        if acc_balance - pending_margin < p_bot.MARGIN_USDT:
            return

        current_syms = {p["symbol"] for p in current_positions}
        if r["inst_id"] in current_syms or r["inst_id"] in _fast_track_opened:
            return
        
        if r["score"] < FAST_TRACK_SCORE: # redundant but safe
            return
        
        if time.time() - FAST_TRACK_COOLDOWN.get(r["inst_id"], 0) < FAST_TRACK_COOLDOWN_SECONDS:
            return
        
        # ── Readiness Check for Fast-Track (TUI Parity) ────────────
        # If symbol just stopped out, require higher conviction (R >= 0.70)
        stop_ctx = _stop_contexts.get(r["inst_id"])
        if stop_ctx:
            curr_r, _ = get_readiness_for_symbol(r["inst_id"], stop_ctx, r)
            if curr_r < 0.70:
                tui_log(f"FT BLOCK: {r['inst_id']} readiness R={curr_r:.2f} < 0.70 threshold for Fast-Track re-entry", event_type="SKIP")
                return

        _fast_track_opened.add(r["inst_id"])
        FAST_TRACK_COOLDOWN[r["inst_id"]] = time.time()

    tui_log(f"⚡ FAST-TRACK: {r['inst_id']} score {r['score']}! (λ={intensity:.2f})")
    
    # ── Wait & Verify ────────────────────────────────────
    verified_result = verify_sim_candidate(r["inst_id"], direction, r["score"])
    
    if verified_result:
        execute_sim_setup(verified_result, direction)
    
    with _fast_track_lock:
        if r["inst_id"] in _fast_track_opened:
            _fast_track_opened.remove(r["inst_id"])


def sim_bot_loop(args) -> None:
    """The main scan-and-execute loop for the simulation bot."""
    global _entropy_penalty, COOLDOWN_SECONDS
    
    # Calculate dynamic cooldown based on timeframe and cooldown argument (T3-16)
    tf_sec = p_bot.get_tf_seconds(args.timeframe)
    COOLDOWN_SECONDS = args.cooldown * tf_sec
    logger.info(f"Simulation cooldown set to {COOLDOWN_SECONDS}s ({args.cooldown} candles)")
    
    cfg = {
        "MIN_VOLUME":     args.min_vol,
        "TIMEFRAME":      args.timeframe,
        "TOP_N":          50,
        "MIN_SCORE":      0,
        "MAX_WORKERS":    args.workers,
        "RATE_LIMIT_RPS": args.rate,
    }

    _ensure_ws_started()
    load_sim_cooldowns()

    # --- Cinematic Boot ---
    play_animation(animations.boot)

    acc = load_paper_account()
    for p in acc.get("positions", []):
        _subscribe_symbol(p["symbol"])

    with _display_lock:
        global _display_thread_running
        if not _display_thread_running:
            _display_thread_running = True
            threading.Thread(target=_live_pnl_display, daemon=True).start()

    while True:
        update_pnl_and_stops()

        positions      = get_sim_positions()
        # Recompute dynamic max positions and available slots
        acc            = load_paper_account()
        acc_balance    = acc.get("balance", 0.0)
        dynamic_max    = p_bot.get_dynamic_max_positions(acc_balance)
        available_slots = dynamic_max - len(positions)

        if available_slots > 0:
            tui_log(f"Scanning LIVE market ({args.timeframe})...")
            _display_paused.set()
            t0 = time.time()
            long_r, short_r = p_bot.run_scanner_both(cfg, args, on_result=on_scan_result)
            elapsed = time.time() - t0
            _display_paused.clear()
            tui_log(f"Scan complete in {elapsed:.1f}s — L: {len(long_r)}  S: {len(short_r)}")

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
                tui_log(f"ENTROPY DEFLATOR: Raising min_score by +{_entropy_penalty} (Saturation: {n_hits}/{total_universe}, Imbalance: {imbalance:.2f})")

            # Calculate dynamic threshold for this scan cycle
            eff_min_score = args.min_score + _entropy_penalty
            if not args.no_dynamic:
                all_scores = [r["score"] for r in (long_r + short_r)]
                dynamic_min = pc.calc_dynamic_threshold(all_scores, args.min_score)
                eff_min_score = max(eff_min_score, dynamic_min)
                
            if eff_min_score > args.min_score:
                tui_log(f"ADAPTIVE FILTER: Effective min_score = {eff_min_score} (Penalty: +{_entropy_penalty})")

            now_dt = datetime.datetime.now()

            fresh_long  = [r for r in long_r  if is_fresh(r, now_dt)]
            fresh_short = [r for r in short_r if is_fresh(r, now_dt)]

            in_pos_updated   = {p["symbol"] for p in get_sim_positions()}
            available_updated = dynamic_max - len(get_sim_positions())

            candidates = p_bot.pick_candidates(
                fresh_long, fresh_short,
                min_score=eff_min_score,
                min_score_gap=args.min_score_gap,
                direction_filter=args.direction,
                in_position=in_pos_updated,
                available_slots=available_updated,
            )

            if candidates:
                tui_log(f"Picked {len(candidates)} candidate(s).")
                for res, direction in candidates:
                    # Account for positions already open PLUS those currently being verified by fast-track
                    with _fast_track_lock:
                        if len(get_sim_positions()) + len(_fast_track_opened) >= dynamic_max:
                            break
                    
                    # ── Wait & Verify ────────────────────────────────────
                    verified_result = verify_sim_candidate(res["inst_id"], direction, res["score"])
                    if verified_result:
                        execute_sim_setup(verified_result, direction)
            else:
                tui_log("No qualifying setups found.")

        current_slots  = dynamic_max - len(get_sim_positions())
        sleep_interval = 5 if current_slots > 0 else args.interval

        if current_slots == 0:
            next_scan = datetime.datetime.now() + datetime.timedelta(seconds=sleep_interval)
            tui_log(f"Waiting until {next_scan.strftime('%H:%M:%S')}")

        _slot_available_event.wait(timeout=sleep_interval)
        _slot_available_event.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Parses arguments and starts the simulation bot."""
    parser = argparse.ArgumentParser(description="Phemex Sim Bot (Paper Trading)")
    parser.add_argument("--interval",       type=int,   default=300)
    parser.add_argument("--min-score",      type=int,   default=125)
    parser.add_argument("--min-score-gap",  type=int,   default=30)
    parser.add_argument("--direction",      default="BOTH", choices=["LONG", "SHORT", "BOTH"])
    parser.add_argument("--timeframe",      default="4H")
    parser.add_argument("--cooldown",       type=int,   default=4, help="Cooldown in candles after exit")
    parser.add_argument("--min-vol",        type=int,   default=1_000_000)
    parser.add_argument("--workers",        type=int,   default=30)
    parser.add_argument("--rate",           type=float, default=20.0)
    parser.add_argument("--no-ai",          action="store_true")
    parser.add_argument("--no-entity",      action="store_true")
    parser.add_argument("--no-dynamic",     action="store_true")
    args = parser.parse_args()

    print(Fore.GREEN + Style.BRIGHT + "  🚀 Phemex SIMULATION Bot Starting (Paper Trading)")
    print(f"  Market   : LIVE (api.phemex.com)")
    print(f"  Account  : LOCAL (paper_account.json)")
    print(f"  Balance  : {INITIAL_BALANCE} USDT")
    print(f"  Interval : {args.interval}s")
    print(f"  Score    : {args.min_score} (gap: {args.min_score_gap})  Direction: {args.direction}\n")

    try:
        sim_bot_loop(args)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n  Bot stopped by user. Shutting down...")
        # Signal the display thread to stop and wait for it
        if _display_thread_running:
            _display_paused.set() # Signal to stop drawing
            # _live_pnl_display is a daemon thread, so it will be killed automatically on exit.
            # However, explicitly signalling might be good practice if it had complex cleanup.
            
        # Ensure WebSocket client is closed if it was started
        if _ws_app:
            try:
                _ws_app.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")

        print(Fore.YELLOW + "  Shutdown complete.")


if __name__ == "__main__":
    main()
