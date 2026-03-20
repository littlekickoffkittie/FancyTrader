#!/usr/bin/env python3
"""
Phemex Common Infrastructure
----------------------------
Shared utilities, API wrappers, and indicators for Phemex scanners.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Exchange Constants ───────────────────────────────────────────────
TAKER_FEE = 0.0006  # 0.06% standard taker fee for Phemex contracts
SCORE_FAST_TRACK = 130 # Score threshold for immediate entry (Fast-Track)

BANNER = r"""
$$$$$$$$\                           $$$$$$$\  $$\                                                 $$$$$$$\   $$$$$$\ $$$$$$$$\ 
$$  _____|                          $$  __$$\ $$ |                                                $$  __$$\ $$  __$$\\\__$$  __|
$$ |   $$$$$$\  $$$$$$$\   $$$$$$\  $$ |  $$ |$$ | $$$$$$\  $$$$$$$\  $$$$$$$\  $$\   $$\         $$ |  $$ |$$ /  $$ |  $$ |   
$$$$$\ \____$$\ $$  __$$\ $$  __$$\ $$$$$$$\ |$$ |$$  __$$\ $$  __$$\ $$  __$$\ $$ |  $$ |$$$$$$\ $$$$$$$\ |$$ |  $$ |  $$ |   
$$  __|$$$$$$$ |$$ |  $$ |$$ /  $$ |$$  __$$\ $$ |$$$$$$$$ |$$ |  $$ |$$ |  $$ |$$ |  $$ |\______|$$  __$$\ $$ |  $$ |  $$ |   
$$ |  $$  __$$ |$$ |  $$ |$$ |  $$ |$$ |  $$ |$$ |$$   ____|$$ |  $$ |$$ |  $$ |$$ |  $$ |        $$ |  $$ |$$ |  $$ |  $$ |   
$$ |  \$$$$$$$ |$$ |  $$ |\$$$$$$$ |$$$$$$$  |$$ |\$$$$$$$\ $$ |  $$ |$$ |  $$ |\$$$$$$$ |        $$$$$$$  | $$$$$$  |  $$ |   
\__|   \_______|\__|  \__| \____$$ |\_______/ \__| \_______|\__|  \__|\__|  \__| \____$$ |        \_______/  \______/   \__|   
                          $$\   $$ |                                            $$\   $$ |                                     
                          \$$$$$$  |                                            \$$$$$$  |                                     
                           \______/                                              \______/
"""

import numpy as np
import requests
from colorama import Fore, Style
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------
# CONFIG & CONSTANTS
# ----------------------------
BASE_URL = os.getenv("PHEMEX_BASE_URL", "https://api.phemex.com")

TIMEFRAME_MAP = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200,
    "1D": 86400, "1W": 604800,
}

DEFAULTS = {
    "MIN_VOLUME": int(os.getenv("MIN_VOLUME", 1_000_000)),
    "TIMEFRAME": os.getenv("TIMEFRAME", "15m"),
    "TOP_N": int(os.getenv("TOP_N", 20)),
    "MIN_SCORE": int(os.getenv("MIN_SCORE", 130)),
    "MAX_WORKERS": int(os.getenv("MAX_WORKERS", 100)),
    "RATE_LIMIT_RPS": float(os.getenv("RATE_LIMIT_RPS", 20.0)),
}

# ----------------------------
# System Audit Logger
# ----------------------------
SYSTEM_AUDIT_LOG = Path(os.path.dirname(os.path.abspath(__file__))) / "system_audit.log"

def log_system_event(event_type: str, message: str, level: int = logging.INFO):
    """
    Logs a high-level system event to the audit log file and the main logger.
    This ensures a permanent record of all significant system actions.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{timestamp}] [{event_type.upper()}] {message}"
    
    # 1. Log to the audit file manually to ensure it's always documented
    try:
        with open(SYSTEM_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(formatted_msg + "\n")
    except Exception as e:
        logging.getLogger("phemex_common").error(f"Failed to write to audit log: {e}")

    # 2. Also log via the standard logging system so it appears in TUI
    # We use a special logger name to avoid infinite loops if setup_colored_logging uses this
    audit_logger = logging.getLogger("system_audit")
    if level == logging.ERROR:
        audit_logger.error(message)
    elif level == logging.WARNING:
        audit_logger.warning(message)
    else:
        audit_logger.info(message)

# ── Centralised score thresholds ──────────────────────────────────────────────
# Single source of truth for all score gating across p_bot, sim_bot, backtest,
# and the scanner modules. Override any via @.env / args at call sites.
SCORE_MIN_DEFAULT    = int(os.getenv("MIN_SCORE", 130))      # standard gate
SCORE_MIN_HTF_BYPASS = int(os.getenv("MIN_SCORE_HTF", 120))  # lower bar with HTF alignment
SCORE_MIN_LOW_LIQ    = int(os.getenv("MIN_SCORE_LOW_LIQ", 145)) # higher bar for low-liquidity assets
SCORE_FAST_TRACK     = int(os.getenv("FAST_TRACK_SCORE", 130)) # immediate-entry threshold
SCORE_EXIT_SIGNAL    = int(os.getenv("EXIT_SIGNAL_SCORE", 100)) # opposite-signal exit threshold
SCORE_GRADE_A        = 75  # grade() boundary
SCORE_GRADE_B        = 60  # grade() boundary
SCORE_GRADE_C        = 45  # grade() boundary

CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ENTITY_API_KEY = os.getenv("ENTITY_API_KEY")
ENTITY_API_BASE_URL = os.getenv("ENTITY_API_BASE_URL", "https://acoustic-trade-scan-now.base44.app")
ENTITY_APP_ID = os.getenv("ENTITY_APP_ID", "69a3845341f04ab2db0682fb")

logger = logging.getLogger("phemex_common")
logger.addHandler(logging.NullHandler())

from collections import deque

# ----------------------------
# Colored Logging
# ----------------------------
class LogBufferHandler(logging.Handler):
    """Custom logging handler that stores the last N formatted logs in a buffer."""
    def __init__(self, buffer: deque):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            self.handleError(record)

class ColoredFormatter(logging.Formatter):
    """Custom logging formatter that adds color to different log levels."""
    
    COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, Fore.WHITE)
        record.levelname = f"{color}{record.levelname}{Style.RESET_ALL}"
        record.msg = f"{color}{record.msg}{Style.RESET_ALL}"
        return super().format(record)

def setup_colored_logging(logger_name: str, level: int = logging.INFO, log_file: Optional[str] = None, buffer: Optional[deque] = None):
    """Sets up a logger with a colored console handler, optional file handler, and optional buffer handler."""
    l = logging.getLogger(logger_name)
    l.setLevel(level)
    
    # Avoid duplicate handlers
    if l.hasHandlers():
        l.handlers.clear()

    # Formatter
    formatter = ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    # Console Handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    l.addHandler(ch)

    # Buffer Handler (for TUI)
    if buffer is not None:
        bh = LogBufferHandler(buffer)
        bh.setFormatter(formatter)
        l.addHandler(bh)

    # File Handler
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        l.addHandler(fh)
    
    return l

from dataclasses import dataclass, field

# ----------------------------
# Data classes
# ----------------------------
@dataclass
class TickerData:
    inst_id: str
    price: float
    rsi: Optional[float]
    prev_rsi: Optional[float]
    bb: Optional[Dict[str, float]]
    ema21: Optional[float]
    change_24h: Optional[float]
    funding_rate: Optional[float]
    patterns: List[Tuple[str, int, float]]
    # These fields accommodate both directions
    dist_low_pct: Optional[float] = None
    dist_high_pct: Optional[float] = None
    vol_spike: float = 1.0
    has_div: bool = False
    rsi_1h: Optional[float] = None
    rsi_4h: Optional[float] = None
    fr_change: float = 0.0
    spread: Optional[float] = None
    dist_to_node_below: Optional[float] = None   # Support
    dist_to_node_above: Optional[float] = None   # Resistance
    ema_slope: Optional[float] = None
    slope_change: Optional[float] = None
    news_count: int = 0
    news_titles: List[str] = field(default_factory=list)
    raw_ohlc: List[Tuple[float, float, float, float]] = field(default_factory=list)
    vol_24h: float = 0.0
    regime: str = "UNKNOWN"
    entropy: float = 0.0
    kalman_slope: float = 0.0

# ----------------------------
# Thread-local session
# ----------------------------
_thread_local = threading.local()

_news_cache: Dict[str, Tuple[int, List[str]]] = {}
_news_cache_lock = threading.Lock()
_news_rate_lock = threading.Lock()
_news_last_request = [0.0]
NEWS_RATE_LIMIT_SECONDS = 1.1

def build_session(timeout: int = 15, max_retries: int = 3) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
    })
    retry = Retry(
        total=max_retries,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

def get_thread_session() -> requests.Session:
    if getattr(_thread_local, "session", None) is None:
        _thread_local.session = build_session()
    return _thread_local.session

# ----------------------------
# Rate limiting
# ----------------------------
_rate_lock = threading.Lock()
_last_request_time_global = 0.0
_global_backoff_until = 0.0  # Timestamp until which all requests are paused

def throttle(rps: float) -> None:
    """Sleep as needed to respect the global requests-per-second limit and backoff."""
    if not rps or rps <= 0:
        return
    interval = 1.0 / rps
    global _last_request_time_global, _global_backoff_until
    
    # 1. Handle global backoff — read atomically under the lock
    with _rate_lock:
        backoff_until = _global_backoff_until
    
    now = time.time()
    if now < backoff_until:
        time.sleep(backoff_until - now)

    # 2. Handle rate limiting
    with _rate_lock:
        now = time.time()
        wait_until = _last_request_time_global + interval
        if now < wait_until:
            sleep_time = wait_until - now
            _last_request_time_global = wait_until
        else:
            sleep_time = 0
            _last_request_time_global = now
            
    if sleep_time > 0.001:
        time.sleep(sleep_time)

def safe_request(method: str, url: str, params: dict = None, json_data: dict = None,
                 headers: dict = None, rps: float = None, timeout: int = 12,
                 stream: bool = False) -> Optional[requests.Response]:
    global _global_backoff_until
    try:
        if rps:
            throttle(rps)
        
        # Double check backoff outside the lock
        now = time.time()
        if now < _global_backoff_until:
            time.sleep(_global_backoff_until - now)

        sess = get_thread_session()
        resp = sess.request(method, url, params=params, json=json_data,
                            headers=headers, timeout=timeout, stream=stream)
        
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("x-ratelimit-retry-after-Contract")
            wait = float(retry_after) if retry_after else 5.0
            
            # Set global backoff immediately
            with _rate_lock:
                _global_backoff_until = time.time() + wait
            
            logger.warning(f"Rate Limit (429) on {url}. Global backoff for {wait}s")
            
            time.sleep(wait)
            # Retry once
            resp = sess.request(method, url, params=params, json=json_data,
                                headers=headers, timeout=timeout, stream=stream)
            
            if resp.status_code == 429:
                return None
        
        if resp.status_code >= 400:
            logger.error(f"HTTP {resp.status_code} on {url}: {resp.text[:200]}")
            return None

        resp.raise_for_status()
        return resp
    except Exception as e:
        # Changed to warning so it's visible without debug mode if things are failing
        logger.warning(f"Request failed: {method} {url} -> {e}")
        return None

# ----------------------------
# Simple TTL cache
# ----------------------------
class SimpleCache:
    def __init__(self, ttl: float = 30.0, max_size: int = 1000):
        self._data: Dict[str, Tuple[float, float, Any]] = {}  # (timestamp, ttl, value)
        self._ttl = float(ttl)
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str, ttl_override: float = None):
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            ts, item_ttl, val = entry
            # Use the TTL stored with the item, or override it if requested
            effective_ttl = ttl_override if ttl_override is not None else item_ttl
            if time.time() - ts > effective_ttl:
                del self._data[key]
                return None
            return val

    def set(self, key: str, val: Any, ttl_override: float = None):
        with self._lock:
            # Check for size limit before adding
            if len(self._data) >= self._max_size and key not in self._data:
                # Remove the oldest entry
                oldest_key = min(self._data.keys(), key=lambda k: self._data[k][0])
                del self._data[oldest_key]
                
            item_ttl = ttl_override if ttl_override is not None else self._ttl
            self._data[key] = (time.time(), item_ttl, val)

CACHE = SimpleCache(ttl=30.0)

# ----------------------------
# Numeric helpers
# ----------------------------
def pct_change(new: float, base: float) -> float:
    try:
        if not base or not math.isfinite(base):
            return 0.0
        return (new - base) / base * 100.0
    except Exception:
        return 0.0

def fmt_vol(v: float) -> str:
    try:
        v = float(v)
    except Exception:
        return str(v)
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:.2f}"

def grade(score: int) -> Tuple[str, str]:
    if score >= SCORE_GRADE_A:
        return "A", Fore.GREEN
    if score >= SCORE_GRADE_B:
        return "B", Fore.LIGHTGREEN_EX
    if score >= SCORE_GRADE_C:
        return "C", Fore.YELLOW
    return "D", Fore.RED

def calc_dynamic_threshold(scores: List[int], default_min: int, percentile: int = 90) -> int:
    """
    Calculate a dynamic score threshold based on the distribution of scores.
    Returns the higher of the percentile value or the default minimum.
    """
    if not scores:
        return default_min
    
    # Use numpy to get the percentile, then floor to int
    dynamic_min = int(np.percentile(scores, percentile))
    return max(dynamic_min, default_min)

# ----------------------------
# Indicator calculations
# ----------------------------
def calc_rsi(closes: List[float], period: int = 14) -> Tuple[Optional[float], Optional[float], List[Optional[float]]]:
    n = len(closes)
    if n <= period:
        return None, None, [None] * n

    arr = np.asarray(closes, dtype=float)
    diffs = np.diff(arr)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    avg_gain = float(gains[:period].sum() / period)
    avg_loss = float(losses[:period].sum() / period)
    history: List[Optional[float]] = [None] * period

    def rs_to_rsi(g: float, l: float) -> float:
        if l == 0.0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    history.append(rs_to_rsi(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        history.append(rs_to_rsi(avg_gain, avg_loss))

    current = history[-1]
    prev = history[-2] if len(history) >= 2 else None
    return current, prev, history

def calc_bb(closes: List[float], period: int = 21, mult: float = 2.0) -> Optional[Dict[str, float]]:
    if len(closes) < period:
        return None
    window = np.asarray(closes[-period:], dtype=float)
    mid = float(window.mean())
    # Use population std (ddof=0) — industry convention for Bollinger Bands
    std = float(np.std(window, ddof=0))
    upper = mid + mult * std
    lower = mid - mult * std
    width_pct = (2.0 * mult * std / mid * 100.0) if mid != 0.0 else 0.0
    return {"upper": upper, "mid": mid, "lower": lower, "std": std, "width_pct": width_pct}

def calc_ema_series(closes: List[float], period: int = 21) -> List[float]:
    n = len(closes)
    if n < period:
        return []
    k = 2.0 / (period + 1.0)
    ema = float(sum(closes[:period]) / period)
    series = [ema]
    for price in closes[period:]:
        ema = (price - ema) * k + ema
        series.append(ema)
    return series

def calc_ema_slope(series: List[float], lookback: int = 3) -> Tuple[Optional[float], Optional[float]]:
    if not series or len(series) <= lookback:
        return None, None
    recent = np.asarray(series[-(lookback + 1):], dtype=float)
    prevs = recent[:-1]
    currs = recent[1:]
    with np.errstate(divide='ignore', invalid='ignore'):
        slopes = np.where(prevs != 0.0, (currs - prevs) / prevs * 100.0, 0.0)
    if slopes.size == 0:
        return None, None
    last_slope = float(slopes[-1])
    delta = float(slopes[-1] - slopes[-2]) if slopes.size > 1 else None
    return last_slope, delta

def calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n <= period:
        return None
    highs_a = np.asarray(highs, dtype=float)
    lows_a = np.asarray(lows, dtype=float)
    closes_a = np.asarray(closes, dtype=float)
    tr_list = []
    for i in range(1, n):
        h_l = highs_a[i] - lows_a[i]
        h_pc = abs(highs_a[i] - closes_a[i - 1])
        l_pc = abs(lows_a[i] - closes_a[i - 1])
        tr = float(max(h_l, h_pc, l_pc))
        tr_list.append(tr)
    if len(tr_list) < period:
        return None
    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
    return atr

def calc_market_regime(closes: List[float], period: int = 20) -> Tuple[str, float]:
    """
    Returns (regime, entropy) where regime is 'TRENDING', 'RANGING', or 'VOLATILE'
    entropy is 0.0 (pure trend) to ~3.5+ (pure chaos)
    """
    if len(closes) < period + 1:
        return "UNKNOWN", 0.0
    
    returns = [
        (closes[i] - closes[i-1]) / closes[i-1]
        for i in range(len(closes) - period, len(closes))
    ]
    
    # Bin returns into 8 buckets
    bins = 8
    min_r, max_r = min(returns), max(returns)
    span = max_r - min_r or 1e-10
    
    counts = [0] * bins
    for r in returns:
        idx = min(bins - 1, int((r - min_r) / span * bins))
        counts[idx] += 1
    
    # Shannon entropy
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / period
            entropy -= p * math.log2(p)
    
    max_entropy = math.log2(bins)  # ~3.0 for 8 bins
    
    if entropy < max_entropy * 0.45:
        regime = "TRENDING"
    elif entropy > max_entropy * 0.80:
        regime = "VOLATILE"
    else:
        regime = "RANGING"
    
    return regime, round(entropy, 4)

def calc_kalman_series(
    closes: List[float],
    process_noise: float = 1e-4,   # Q — how much true price can change
    measurement_noise: float = 1e-2 # R — how noisy observations are
) -> List[float]:
    """
    Kalman filter price smoother.
    Lower R = trusts price more (tracks faster).
    Higher R = trusts model more (smoother, lags more).
    Adaptive: automatically adjusts in volatile conditions.
    """
    if not closes:
        return []
    
    x = closes[0]   # initial state estimate
    P = 1.0          # initial error covariance
    Q = process_noise
    R = measurement_noise
    result = [x]
    
    for z in closes[1:]:
        # Predict
        P = P + Q
        # Update
        K = P / (P + R)          # Kalman gain
        x = x + K * (z - x)     # state update
        P = (1 - K) * P          # covariance update
        result.append(x)
    
    return result

def calc_kelly_margin(
    bankroll: float,
    win_rate: float,      # e.g. 0.58 for 58%
    avg_win: float,       # average winning trade PnL
    avg_loss: float,      # average losing trade PnL (positive number)
    fraction: float = 0.5 # half-Kelly is safer
) -> float:
    # Not enough history yet or no edge — use flat 2% of bankroll fallback
    if avg_loss == 0 or avg_win == 0 or win_rate <= 0:
        logging.getLogger("phemex_common").debug("Kelly: Insufficient history, fallback to 2% margin")
        return round(bankroll * 0.02, 2)
    
    b = avg_win / avg_loss
    q = 1 - win_rate
    kelly = (win_rate * b - q) / b
    
    # Kelly went negative — no edge detected yet, use flat fallback
    if kelly <= 0:
        logging.getLogger("phemex_common").debug(f"Kelly: Negative edge ({kelly:.4f}), fallback to 2% margin")
        return round(bankroll * 0.02, 2)
    
    margin = bankroll * kelly * fraction
    # Hard cap at 10% bankroll to prevent massive drawdowns from single trade outliers
    return round(min(margin, bankroll * 0.1), 2)

def calc_volume_profile(ohlc: List[Tuple[float, float, float, float]], volumes: List[float], bins: int = 20) -> Tuple[Optional[float], List[float]]:
    if not ohlc or not volumes or len(ohlc) != len(volumes):
        return None, []
    highs = [c[1] for c in ohlc]
    lows = [c[2] for c in ohlc]
    min_p = min(lows)
    max_p = max(highs)
    if min_p == max_p:
        return min_p, []
    bin_size = (max_p - min_p) / bins
    profile = [0.0] * bins
    for (o, h, l, c), v in zip(ohlc, volumes):
        lo_bin = max(0, int((l - min_p) / bin_size))
        hi_bin = min(bins - 1, int((h - min_p) / bin_size))
        span = max(1, hi_bin - lo_bin + 1)
        for b in range(lo_bin, hi_bin + 1):
            profile[b] += v / span
    max_vol = max(profile)
    if max_vol <= 0.0:
        # Fallback to first bin if all volumes are effectively zero
        poc_idx = 0
        return min_p + bin_size * (poc_idx + 0.5), []
    poc_idx = profile.index(max_vol)
    poc_price = min_p + bin_size * (poc_idx + 0.5)
    threshold = max_vol * 0.70
    nodes = [min_p + bin_size * (i + 0.5) for i, vol in enumerate(profile) if vol >= threshold]
    return poc_price, nodes

def calc_volume_spike(volumes: List[float], period: int = 20) -> float:
    n = len(volumes)
    if n <= period:
        return 1.0
    trailing = np.asarray(volumes[-(period + 1):-1], dtype=float)
    avg = float(trailing.mean()) if trailing.size > 0 else 0.0
    if avg <= 0.0:
        return 1.0
    latest = float(volumes[-1])
    return latest / avg

# ── New Mathematical Utilities for Adaptive Filtering ────────────────────────

def calc_shannon_entropy_signals(long_count: int, short_count: int, total_scanned: int) -> float:
    """
    Computes Shannon entropy of the signal direction distribution.
    Includes 'NONE' as a category representing no signal.
    H = - sum(p_i * log2(p_i))
    """
    if total_scanned <= 0:
        return 0.0
    
    n_none = max(0, total_scanned - long_count - short_count)
    counts = [long_count, short_count, n_none]
    
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / total_scanned
            entropy -= p * math.log2(p)
            
    return round(entropy, 4)

def calc_hurst_exponent(series: List[float], max_window: int = 50) -> float:
    """
    Estimates the Hurst exponent using a simplified R/S analysis.
    H > 0.5: Persistent (Trending)
    H < 0.5: Anti-persistent (Mean-reverting)
    H = 0.5: Random Walk (Noise)
    """
    if len(series) < 20:
        return 0.5
    
    # Calculate log returns
    arr = np.array(series)
    returns = np.diff(np.log(arr))
    
    def rs_analysis(data):
        if len(data) < 4: return 0.0
        mean = np.mean(data)
        y = np.cumsum(data - mean)
        r = np.max(y) - np.min(y)
        s = np.std(data)
        return r / s if s > 0 else 0.0

    # We use a few window sizes to estimate the slope of log(R/S) vs log(size)
    # For a quick estimation on ~100 candles, we can just use the full range vs half range
    sizes = [len(returns) // 4, len(returns) // 2, len(returns)]
    rs_vals = []
    for s in sizes:
        # Average R/S across non-overlapping windows
        windows = [returns[i:i+s] for i in range(0, len(returns), s) if len(returns[i:i+s]) == s]
        if windows:
            rs_vals.append(np.mean([rs_analysis(w) for w in windows]))
        else:
            rs_vals.append(0.0)
            
    # Filter out zero R/S values
    valid_idx = [i for i, val in enumerate(rs_vals) if val > 0]
    if len(valid_idx) < 2:
        return 0.5
        
    x = np.log([sizes[i] for i in valid_idx])
    y = np.log([rs_vals[i] for i in valid_idx])
    
    # Linear regression slope is H
    slope, _ = np.polyfit(x, y, 1)
    return round(float(np.clip(slope, 0.0, 1.0)), 3)

class HawkesTracker:
    """
    Tracks self-exciting process intensity (Hawkes) for signal directions.
    λ(t) = μ + Σ α * exp(-β * (t - t_i))
    """
    def __init__(self, mu: float = 0.1, alpha: float = 0.8, beta: float = 0.1):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.last_time = time.time()
        self.intensity = mu
        self._lock = threading.Lock()

    def update(self, event_occurred: bool = True) -> float:
        """Decays intensity and adds pulse if event occurred."""
        with self._lock:
            now = time.time()
            dt = now - self.last_time
            # Decay intensity towards baseline mu
            self.intensity = self.mu + (self.intensity - self.mu) * math.exp(-self.beta * dt)
            
            if event_occurred:
                self.intensity += self.alpha
                
            self.last_time = now
            return self.intensity

    def get_intensity(self) -> float:
        return self.update(event_occurred=False)

@dataclass
class StopContext:
    timestamp: float
    price: float
    atr: float
    spread: float
    direction: str = "LONG" # "LONG" or "SHORT"
    stop_count: int = 1     # Number of consecutive stops

def calc_readiness_score(
    symbol: str,
    stop_ctx: StopContext,
    current_atr: float,
    current_spread: float,
    bid_vol: float,
    ask_vol: float,
    candles: List[Tuple[float, float, float, float]], # [open, high, low, close]
    buy_vol: Optional[float] = None, # Taker buy volume over window
    sell_vol: Optional[float] = None, # Taker sell volume over window
    half_life_h: float = 4.0,
    weights: Dict[str, float] = None,
    intended_direction: Optional[str] = None
) -> float:
    """
    Computes the Readiness Score R ∈ [0, 1] based on five factors:
    Time, Volatility, Order Book, Price Behavior, and Volume Delta.
    """
    if weights is None:
        weights = {
            "time": 0.20,
            "vol": 0.30,
            "book": 0.20,
            "price": 0.20,
            "vdelta": 0.10
        }
    
    # Use intended direction if provided, else fallback to the one in context
    dir_to_use = intended_direction or stop_ctx.direction
    
    # 1. Factor 1: Time Decay (F_time)
    # Consecutive stops increase the required wait time (effective half-life)
    s_count = getattr(stop_ctx, 'stop_count', 1)
    # Cap stop count at 5 for scaling to avoid infinite wait
    effective_half_life = half_life_h * min(s_count, 5)
    
    t_hours = (time.time() - stop_ctx.timestamp) / 3600.0
    f_time = 1.0 - math.exp(-(t_hours * math.log(2)) / effective_half_life)
    
    # 2. Factor 2: Volatility (F_vol)
    # ATR ratio: current vs at stop time
    vol_ratio = current_atr / stop_ctx.atr if stop_ctx.atr > 0 else 1.0
    f_vol = max(0.0, min(1.0, 2.0 - vol_ratio))
    
    # 3. Factor 3: Order Book (F_book)
    # Spread sub-signal
    spread_ratio = current_spread / stop_ctx.spread if stop_ctx.spread > 0 else 1.0
    s_spread = max(0.0, min(1.0, 2.0 - spread_ratio))
    # Imbalance sub-signal
    total_book_vol = bid_vol + ask_vol
    if dir_to_use == "LONG":
        s_imbalance = ((bid_vol - ask_vol) / total_book_vol * 0.5 + 0.5) if total_book_vol > 0 else 0.5
    else:
        s_imbalance = ((ask_vol - bid_vol) / total_book_vol * 0.5 + 0.5) if total_book_vol > 0 else 0.5
    f_book = 0.5 * s_spread + 0.5 * s_imbalance
    
    # 4. Factor 4: Price Behaviour (F_price)
    # G: Favorable candle ratio
    if dir_to_use == "LONG":
        fav_count = sum(1 for o, h, l, c in candles if c > o)
    else:
        fav_count = sum(1 for o, h, l, c in candles if c < o)
    g = fav_count / len(candles) if candles else 0.5

    # L: Adverse movement penalty (Relative to the stop-out level)
    if dir_to_use == "LONG":
        adverse_count = sum(1 for o, h, l, c in candles if l < stop_ctx.price)
    else:
        adverse_count = sum(1 for o, h, l, c in candles if h > stop_ctx.price)
    l_penalty = adverse_count / len(candles) if candles else 0.0

    # V: Recovery/Validation bonus
    # For LONG: Current price > Stop level is a recovery.
    # For SHORT: Current price < Stop level is a "recovery" (returning to thesis zone).
    current_price = candles[-1][3] if candles else 0.0
    if dir_to_use == "LONG":
        v_bonus = 0.2 if current_price > stop_ctx.price else 0.0
    else:
        v_bonus = 0.2 if current_price < stop_ctx.price else 0.0
        
    f_price = 0.5 * g + 0.3 * (1.0 - l_penalty) + 0.2 * v_bonus
    f_price = max(0.0, min(1.0, f_price))
    
    # 5. Factor 5: Volume Delta (F_vdelta)
    if buy_vol is not None and sell_vol is not None:
        total_v = buy_vol + sell_vol
        f_vdelta = ((buy_vol - sell_vol) / total_v * 0.5 + 0.5) if total_v > 0 else 0.5
    else:
        # If no volume data, redistribute F_vdelta weight to vol and price
        weights["vol"] += 0.05
        weights["price"] += 0.05
        f_vdelta = 0.0
        weights["vdelta"] = 0.0
        
    r = (
        weights["time"] * f_time +
        weights["vol"] * f_vol +
        weights["book"] * f_book +
        weights["price"] * f_price +
        weights["vdelta"] * f_vdelta
    )
    
    # Apply direct penalty based on stop count
    if s_count > 1:
        # Penalty of 0.1 for each stop beyond the first, cap at 0.4
        direct_penalty = min(0.4, (s_count - 1) * 0.1)
        r -= direct_penalty
    
    return round(max(0.0, min(1.0, r)), 4)

def get_readiness_scalar(r: float) -> float:
    """Returns the position size scalar based on Readiness Score R."""
    if r < 0.35:
        return 0.0
    if r >= 0.70:
        return 1.0
    # Linear ramp from 0% to 75% between 0.35 and 0.70
    return (r - 0.35) / (0.70 - 0.35) * 0.75

# ----------------------------
# Phemex API helpers
# ----------------------------
def _resolve_resolution(timeframe: str) -> int:
    return TIMEFRAME_MAP.get(timeframe, 900)

def get_tickers(rps: float = None) -> List[Dict[str, Any]]:
    """
    Fetch all USDT-M perpetual 24hr tickers.
    Endpoint: GET /md/v3/ticker/24hr/all
    """
    url = f"{BASE_URL}/md/v3/ticker/24hr/all"
    resp = safe_request("GET", url, rps=rps)
    if not resp:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    if data.get("error") is not None:
        logger.debug("Tickers API error: %s", data.get("error"))
        return []
    result = data.get("result", []) or []
    
    filtered = []
    for t in result:
        if not isinstance(t, dict): continue
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"): continue
        if symbol.startswith("s"): continue 
        
        filtered.append(t)
    
    filtered.sort(key=lambda x: float(x.get("turnoverRv") or 0.0), reverse=True)
    return filtered

def get_candles(symbol: str, timeframe: str = "15m", limit: int = 100, rps: float = None) -> List[List[Any]]:
    """
    Fetch klines from Phemex public market data endpoint.
    Endpoint: GET /exchange/public/md/v2/kline/last
    """
    resolution = _resolve_resolution(timeframe)
    cache_key = f"candles:{symbol}:{resolution}:{limit}"
    
    # Adaptive TTL: 300s (5min) for HTF (>= 1H), 30s for LTF
    effective_ttl = 300.0 if resolution >= 3600 else 30.0
    cached = CACHE.get(cache_key, ttl_override=effective_ttl)
    if cached is not None:
        return cached

    # Standard tradable symbols do NOT use a dot prefix for this endpoint
    api_symbol = symbol.replace(".", "")

    # Map custom limits to allowed Phemex limits: [5, 10, 50, 100, 500, 1000]
    allowed_limits = [5, 10, 50, 100, 500, 1000]
    api_limit = next((l for l in allowed_limits if l >= limit), 1000)

    url = f"{BASE_URL}/exchange/public/md/v2/kline/last"
    params = {"symbol": api_symbol, "resolution": resolution, "limit": api_limit}
    
    try:
        resp = safe_request("GET", url, params=params, rps=rps)
        if not resp:
            logger.error(f"get_candles: No response for {symbol} {timeframe}")
            return []
        data = resp.json()
        
        if data.get("code") == 0:
            rows = data.get("data", {}).get("rows", [])
            if not rows:
                logger.warning(f"get_candles: No rows in data for {symbol} {timeframe}. Full response: {data}")
                return []
            
            rows_sorted = sorted(rows, key=lambda r: r[0])
            # Slice to the exact requested amount
            final_rows = rows_sorted[-limit:]
            CACHE.set(cache_key, final_rows, ttl_override=effective_ttl)
            return final_rows
        else:
            logger.error(f"get_candles: API error {data.get('code')} for {symbol} {timeframe}. Full response: {data}")
            return []
    
    except json.JSONDecodeError as e:
    
        logger.error(f"get_candles: JSON decode error for {symbol} {timeframe}: {e}. Response text: {resp.text if resp else 'N/A'}")
    
        return []
    
    except Exception as e:
    
        logger.error(f"get_candles: Unexpected error for {symbol} {timeframe}: {e}")
    
        return []
    
def get_funding_rate_info(symbol: str, rps: float = None) -> Tuple[Optional[float], Optional[float], float]:
    """
    Fetch current funding rate.
    Endpoint: GET /contract-biz/public/real-funding-rates?symbol=
    Returns (current_fr, prev_fr, delta).
    """
    cache_key = f"funding:{symbol}"
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/contract-biz/public/real-funding-rates"
    resp = safe_request("GET", url, params={"symbol": symbol}, rps=rps)
    if not resp:
        return None, None, 0.0
    try:
        data = resp.json()
    except Exception:
        return None, None, 0.0

    items = data if isinstance(data, list) else data.get("data", [])
    if not items:
        return _get_funding_rate_history(symbol, rps)

    try:
        entry = None
        if isinstance(items, list):
            for it in items:
                if it.get("symbol") == symbol:
                    entry = it
                    break
            if entry is None and items:
                entry = items[0]
        else:
            entry = items

        current_fr = float(entry.get("fundingRate", 0.0))
        out = (current_fr, current_fr, 0.0)
        CACHE.set(cache_key, out)
        return out
    except Exception:
        return None, None, 0.0

def prefetch_all_funding_rates(rps: float = None):
    """
    Fetch all funding rates in one call and populate CACHE.
    """
    url = f"{BASE_URL}/contract-biz/public/real-funding-rates"
    resp = safe_request("GET", url, rps=rps)
    if not resp:
        return
    try:
        data = resp.json()
        res_data = data.get("data", {})
        if isinstance(res_data, list):
            items = res_data
        else:
            items = res_data.get("rows", [])

        if not items:
            logger.debug("Funding prefetch returned empty: %s", data)
            return
        
        populated = 0
        for item in items:
            if not isinstance(item, dict): continue
            sym = item.get("symbol")
            if not sym: continue
            fr_raw = item.get("fundingRate") or item.get("fundingRateRr")
            if fr_raw is not None:
                fr = float(fr_raw)
                CACHE.set(f"funding:{sym}", (fr, fr, 0.0))
                populated += 1
        
        logger.info("Funding prefetch: %d symbols cached", populated)
    except Exception as e:
        logger.error("Funding prefetch error: %s", e)

def _get_funding_rate_history(symbol: str, rps: float = None) -> Tuple[Optional[float], Optional[float], float]:
    base = symbol.replace("USDT", "")
    fr_symbol = f".{base}USDTFR8H"
    url = f"{BASE_URL}/api-data/public/data/funding-rate-history"
    resp = safe_request("GET", url, params={"symbol": fr_symbol, "limit": 2, "latestOnly": False}, rps=rps)
    if not resp:
        return None, None, 0.0
    try:
        data = resp.json()
        if data.get("code") != 0:
            return None, None, 0.0
        rows = data.get("data", {}).get("rows", [])
        if not rows:
            return None, None, 0.0
        current_fr = float(rows[-1].get("fundingRate", 0.0))
        prev_fr = float(rows[-2].get("fundingRate", current_fr)) if len(rows) > 1 else current_fr
        return current_fr, prev_fr, current_fr - prev_fr
    except Exception:
        return None, None, 0.0

def get_order_book(symbol: str, rps: float = None):
    """
    Endpoint: GET /md/v2/orderbook?symbol=
    Returns (best_bid, best_ask, spread_pct, bid_depth, ask_depth).
    """
    url = f"{BASE_URL}/md/v2/orderbook"
    resp = safe_request("GET", url, params={"symbol": symbol}, rps=rps)
    if not resp:
        return None, None, None, 0.0, 0.0
    try:
        data = resp.json()
    except Exception:
        return None, None, None, 0.0, 0.0
    if data.get("error") is not None:
        return None, None, None, 0.0, 0.0

    result = data.get("result", {}) or {}
    book = result.get("orderbook_p", {}) or {}
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None, None, 0.0, 0.0
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except Exception:
        return None, None, None, 0.0, 0.0
    if best_bid == 0.0:
        spread_pct = None
    else:
        spread_pct = (best_ask - best_bid) / best_bid * 100.0

    def depth_sum(entries):
        total = 0.0
        for row in entries[:20]: # Top 20 levels
            try:
                p = float(row[0]); q = float(row[1])
                total += q # sum volume (quantity)
            except Exception:
                continue
        return total

    bid_d = depth_sum(bids)
    ask_d = depth_sum(asks)
    return best_bid, best_ask, spread_pct, bid_d, ask_d

def get_cryptopanic_news(coin_symbol: str) -> Tuple[int, List[str]]:
    if not CRYPTOPANIC_API_KEY:
        return 0, []
    
    with _news_cache_lock:
        if coin_symbol in _news_cache:
            return _news_cache[coin_symbol]
    
    # Use a separate lock for rate limiting to avoid blocking the cache lock
    with _news_rate_lock:
        elapsed = time.time() - _news_last_request[0]
        if elapsed < NEWS_RATE_LIMIT_SECONDS:
            time.sleep(NEWS_RATE_LIMIT_SECONDS - elapsed)
        _news_last_request[0] = time.time()
        
    try:
        url = "https://cryptopanic.com/api/developer/v2/posts/"
        params = {"auth_token": CRYPTOPANIC_API_KEY, "currencies": coin_symbol, "filter": "news"}
        resp = safe_request("GET", url, params=params)
        if not resp:
            return 0, []
        data = resp.json()
        results = data.get("results", []) or []
        count = data.get("count", len(results))
        titles = [res.get("title", "") for res in results[:5]]
        result = (min(count, 99), titles)
    except Exception as e:
        logger.debug("cryptopanic error: %s", e)
        result = (0, [])

    with _news_cache_lock:
        _news_cache[coin_symbol] = result
    return result

# ----------------------------
# AI & Entity integration
# ----------------------------
def unified_analyse(
    ticker: dict, 
    cfg: dict, 
    direction: str, 
    score_func, 
    detect_patterns_func, 
    detect_div_func,
    calc_confidence_func,
    enable_ai: bool = True, 
    enable_entity: bool = True,
    scan_id: Optional[str] = None
) -> Optional[dict]:
    """
    Unified analysis engine for both LONG and SHORT scanners.
    Handles data fetching, indicator calculation, and result construction.
    """
    symbol = ticker.get("symbol")
    if not symbol: return None

    try:
        last   = float(ticker.get("lastRp") or ticker.get("closeRp") or 0.0)
        open24 = float(ticker.get("openRp") or last)
        low24  = float(ticker.get("lowRp") or last)
        high24 = float(ticker.get("highRp") or last)
        vol24  = float(ticker.get("turnoverRv") or 0.0)

        if vol24 < cfg.get("MIN_VOLUME", 1_000_000): return None
        if last == 0.0: return None

        # 1. Funding check (direction-specific thresholds handled in score_func or caller)
        fr, prev_fr, fr_change = get_funding_rate_info(symbol, rps=cfg.get("RATE_LIMIT_RPS"))
        if fr is None:
            fr_raw = ticker.get("fundingRateRr")
            fr = float(fr_raw) if fr_raw is not None else 0.0
            fr_change = 0.0

        # 2. Fetch klines
        candles = get_candles(symbol, timeframe=cfg["TIMEFRAME"], limit=cfg.get("CANDLES", 100), rps=cfg.get("RATE_LIMIT_RPS"))
        if not candles: return None

        ohlc, highs, lows, closes, vols = [], [], [], [], []
        for c in candles:
            try:
                o, h, l, cl = float(c[3]), float(c[4]), float(c[5]), float(c[6])
                v = float(c[7]) if len(c) > 7 else 0.0
                ohlc.append((o, h, l, cl))
                highs.append(h); lows.append(l); closes.append(cl); vols.append(v)
            except Exception: continue
        
        if not closes: return None

        # 3. Indicator calculation
        rsi, prev_rsi, rsi_hist = calc_rsi(closes)
        bb = calc_bb(closes)
        ema_series = calc_ema_series(closes, 21)
        ema21 = ema_series[-1] if ema_series else None
        ema_slope, slope_change = calc_ema_slope(ema_series)
        atr = calc_atr(highs, lows, closes)
        vol_spike = calc_volume_spike(vols)

        # 3b. Advanced Indicators: Regime & Kalman
        regime, entropy = calc_market_regime(closes)
        kalman_series = calc_kalman_series(closes)
        kalman_price  = kalman_series[-1] if kalman_series else None
        kalman_slope  = kalman_series[-1] - kalman_series[-2] if len(kalman_series) >= 2 else 0.0

        # 4. Market data (Orderbook, News)
        best_bid, best_ask, spread, bid_d, ask_d = get_order_book(symbol, rps=cfg.get("RATE_LIMIT_RPS"))
        
        # 5. Volume Profile
        poc_price, nodes = calc_volume_profile(ohlc, vols, bins=20)
        dist_to_node_below, dist_to_node_above = None, None
        if nodes and last > 0:
            nodes_below = [n for n in nodes if n < last]
            nodes_above = [n for n in nodes if n > last]
            if nodes_below: dist_to_node_below = abs(pct_change(last, max(nodes_below)))
            if nodes_above: dist_to_node_above = abs(pct_change(last, min(nodes_above)))

        # 6. HTF Context
        rsi_1h, rsi_4h = None, None
        c1h = get_candles(symbol, timeframe="1H", limit=50, rps=cfg.get("RATE_LIMIT_RPS"))
        if c1h: 
            cl1h = [float(c[6]) for c in c1h]
            if cl1h: rsi_1h, _, _ = calc_rsi(cl1h)
        
        c4h = get_candles(symbol, timeframe="4H", limit=50, rps=cfg.get("RATE_LIMIT_RPS"))
        if c4h:
            cl4h = [float(c[6]) for c in c4h]
            if cl4h: rsi_4h, _, _ = calc_rsi(cl4h)

        # 7. Pattern & Divergence detection
        patterns = detect_patterns_func(ohlc)
        has_div = detect_div_func(closes, rsi_hist)

        # 8. Data Aggregation
        data = TickerData(
            inst_id=symbol, price=last, rsi=rsi, prev_rsi=prev_rsi, bb=bb, ema21=ema21,
            change_24h=pct_change(last, open24), funding_rate=fr, patterns=patterns,
            dist_low_pct=pct_change(last, low24), dist_high_pct=pct_change(last, high24),
            vol_spike=vol_spike, has_div=has_div, rsi_1h=rsi_1h, rsi_4h=rsi_4h,
            fr_change=fr_change or 0.0, spread=spread,
            dist_to_node_below=dist_to_node_below, dist_to_node_above=dist_to_node_above,
            ema_slope=ema_slope, slope_change=slope_change,
            raw_ohlc=ohlc[-10:], vol_24h=vol24,
            regime=regime, entropy=entropy, kalman_slope=kalman_slope
        )

        # 9. Scoring
        score, signals = score_func(data)

        # 10. Result construction
        bb_pct = None
        if bb:
            bb_range = bb["upper"] - bb["lower"]
            if bb_range > 0: bb_pct = (last - bb["lower"]) / bb_range * 100.0

        confidence, conf_color, conf_notes = calc_confidence_func(data, score, bb_pct)
        stop_pct = (0.5 * atr / last * 100.0) if (atr and last > 0) else None

        result = {
            "inst_id": symbol, "price": last, "change_24h": data.change_24h,
            "vol_24h": vol24, "rsi": rsi, "prev_rsi": prev_rsi, "bb_pct": bb_pct,
            "ema21": ema21, "funding_pct": fr * 100.0 if fr is not None else None,
            "score": score, "signals": signals, "patterns": patterns,
            "confidence": confidence, "conf_color": conf_color, "conf_notes": conf_notes,
            "dist_low": data.dist_low_pct, "dist_high": data.dist_high_pct,
            "vol_spike": vol_spike, "bb_width": bb["width_pct"] if bb else 0.0,
            "atr_stop_pct": stop_pct, "raw_ohlc": ohlc[-10:], "spread": spread,
            "dist_to_node_below": dist_to_node_below, "dist_to_node_above": dist_to_node_above,
            "ema_slope": ema_slope, "slope_change": slope_change, "fr_change": fr_change,
            "rsi_1h": rsi_1h, "rsi_4h": rsi_4h, "scan_timestamp": datetime.datetime.now().isoformat(),
            "regime": regime, "entropy": entropy, "kalman_price": kalman_price, "kalman_slope": kalman_slope,
        }

        # 11. Entity API Hook
        if enable_entity and ENTITY_API_KEY:
            pc_res = make_entity_request("ScanResult", method="POST", data={
                "scan_id": scan_id, "timestamp": datetime.datetime.now().isoformat(),
                "inst_id": symbol, "price": last, "change_24h": data.change_24h or 0.0,
                "rsi": rsi or 50.0, "funding_rate": round(fr * 100, 8) if fr is not None else 0.0,
                "score": score, "signals": signals, "atr_stop_pct": stop_pct or 0.0,
                "vol_spike": vol_spike or 0.0, "spread": spread or 0.0, "direction": direction.capitalize()
            })
            if pc_res and isinstance(pc_res, dict): result["entity_id"] = pc_res.get("id")

        return result

    except Exception as e:
        logger.error(f"Error in unified_analyse for {symbol}: {e}")
        return None

def make_entity_request(entity_name: str, method: str = "POST", data: dict = None, entity_id: str = None):
    if not ENTITY_API_KEY:
        return None
    url = f"{ENTITY_API_BASE_URL}/api/apps/{ENTITY_APP_ID}/entities/{entity_name}"
    if entity_id:
        url = f"{url}/{entity_id}"
    headers = {"api_key": ENTITY_API_KEY, "Content-Type": "application/json"}
    try:
        if method.upper() == "GET":
            r = safe_request("GET", url, params=data, headers=headers)
        elif method.upper() == "PUT":
            r = safe_request("PUT", url, json_data=data, headers=headers)
        else:
            r = safe_request("POST", url, json_data=data, headers=headers)
        if not r:
            return None
        return r.json()
    except Exception:
        return None

def call_deepseek(prompt: str, system_prompt: str = "You are an expert crypto trader and technical analyst. Use plain text formatting.", stream: bool = True):
    if not DEEPSEEK_API_KEY:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY.strip()}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        "temperature": 0.7,
        "stream": stream
    }
    try:
        resp = safe_request("POST", url, json_data=payload, headers=headers, stream=stream)
        if not resp:
            return None
        if stream:
            full_text = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8")
                if line_str.startswith("data: "):
                    data_raw = line_str[len("data: "):]
                    if data_raw == "[DONE]":
                        break
                    try:
                        d = json.loads(data_raw)
                        delta = d["choices"][0]["delta"]
                        if "content" in delta:
                            content = delta["content"]
                            print(Fore.LIGHTBLACK_EX + content, end="", flush=True)
                            full_text += content
                    except Exception:
                        continue
            print()
            return full_text
        else:
            d = resp.json()
            return d["choices"][0]["message"]["content"]
    except Exception as e:
        logger.debug("DeepSeek call failed: %s", e)
        return None
