import os
import time
import threading
from typing import Dict, Optional
from dotenv import load_dotenv
from src.core.config_schema import BotConfig
from src.core.config_manager import load_config
from src.core.sim_state import (
    get_open_positions, open_position,
    close_position, log_event, save_snapshot
)

load_dotenv()

# Registry of running sim instances keyed by user_id
_running: Dict[str, threading.Event] = {}
_locks: Dict[str, threading.Lock] = {}


def _get_mark_price(symbol: str) -> Optional[float]:
    """Fetch current mark price from Phemex REST."""
    import urllib.request
    import json
    url = f"https://api.phemex.com/md/v3/ticker/24hr?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        # Phemex V3 ticker response has markRp as a string
        rp = data.get("result", {}).get("markRp")
        if rp is not None:
            return float(rp)
    except Exception:
        pass
    return None


def _get_candidate_symbols(config: BotConfig) -> list:
    """Return symbols to scan — whitelist or a default set."""
    if config.scanner.symbol_whitelist:
        return config.scanner.symbol_whitelist
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XAGUSDT", "PAXGUSDT"]


def _score_symbol(symbol: str, config: BotConfig) -> tuple:
    """
    Returns (score, direction, price, atr_estimate).
    Plug your full signal pipeline in here later.
    """
    price = _get_mark_price(symbol)
    if not price:
        return 0.0, None, None, None

    # Placeholder scoring — replace with real signal pipeline
    import random
    score = random.uniform(40, 100)
    direction = "long" if random.random() > 0.5 else "short"
    atr_estimate = price * 0.005

    return score, direction, price, atr_estimate


def _sim_loop(user_id: str, stop_event: threading.Event) -> None:
    log_event(user_id, "info", "Sim engine started")

    starting_equity = 100.0
    equity = starting_equity
    gross_profit = 0.0
    gross_loss = 0.0
    peak_equity = equity

    while not stop_event.is_set():
        try:
            config = load_config(user_id)

            # --- Check open positions ---
            positions = get_open_positions(user_id)
            for pos in positions:
                symbol = pos["symbol"]
                direction = pos["direction"]
                entry = float(pos["entry_price"])
                sl = float(pos["metadata"]["sl"])
                tp = float(pos["metadata"]["tp"])
                size = float(pos["size_usdt"])
                lev = int(pos["leverage"])

                price = _get_mark_price(symbol)
                if not price:
                    continue

                hit_sl = (direction == "long" and price <= sl) or \
                         (direction == "short" and price >= sl)
                hit_tp = (direction == "long" and price >= tp) or \
                         (direction == "short" and price <= tp)

                if hit_sl or hit_tp:
                    exit_price = tp if hit_tp else sl
                    exit_reason = "tp" if hit_tp else "sl"

                    if direction == "long":
                        pnl_pct = (exit_price - entry) / entry
                    else:
                        pnl_pct = (entry - exit_price) / entry

                    pnl_usdt = pnl_pct * size * lev
                    equity += pnl_usdt

                    close_position(pos["id"], exit_price, pnl_usdt, pnl_pct * 100, exit_reason)
                    log_event(user_id, "info", f"Closed {direction} {symbol} via {exit_reason}",
                              {"pnl_usdt": round(pnl_usdt, 4), "exit_price": exit_price})

                    if pnl_usdt > 0:
                        gross_profit += pnl_usdt
                    else:
                        gross_loss += abs(pnl_usdt)

            # --- Circuit breaker ---
            drawdown = (peak_equity - equity) / peak_equity * 100
            if drawdown >= config.risk.max_drawdown_pct:
                log_event(user_id, "warn", f"Circuit breaker triggered at {drawdown:.1f}% drawdown")
                stop_event.set()
                break

            peak_equity = max(peak_equity, equity)

            # --- Entry scan ---
            positions = get_open_positions(user_id)
            if len(positions) < config.risk.max_positions:
                candidates = _get_candidate_symbols(config)
                open_symbols = {p["symbol"] for p in positions}

                for symbol in candidates:
                    if symbol in config.scanner.symbol_blacklist:
                        continue
                    if symbol in open_symbols:
                        continue

                    score, direction, price, atr = _score_symbol(symbol, config)

                    if score < config.signal.score_gate:
                        continue
                    if not direction:
                        continue
                    if not config.execution.allow_long and direction == "long":
                        continue
                    if not config.execution.allow_short and direction == "short":
                        continue

                    sl_dist = atr * config.risk.stop_loss_atr_multiplier
                    tp_dist = sl_dist * config.risk.take_profit_ratio

                    if direction == "long":
                        sl = price - sl_dist
                        tp = price + tp_dist
                    else:
                        sl = price + sl_dist
                        tp = price - tp_dist

                    open_position(
                        user_id, symbol, direction, price,
                        config.risk.trade_size_usdt,
                        config.risk.leverage,
                        sl, tp, score
                    )
                    log_event(user_id, "info", f"Opened {direction} {symbol} @ {price}",
                              {"score": score, "sl": sl, "tp": tp})

                    open_symbols.add(symbol)
                    if len(open_symbols) >= config.risk.max_positions:
                        break

            # --- Snapshot ---
            closed = _get_closed_count(user_id)
            wins = _get_win_count(user_id)
            win_rate = round(wins / closed * 100, 2) if closed else 0.0
            pf = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0
            dd = round((peak_equity - equity) / peak_equity * 100, 2)
            save_snapshot(user_id, equity, equity - starting_equity, win_rate, pf, dd, closed)

        except Exception as e:
            log_event(user_id, "error", f"Sim loop error: {e}")

        stop_event.wait(config.scanner.scan_interval_seconds)

    log_event(user_id, "info", "Sim engine stopped")


def _get_closed_count(user_id: str) -> int:
    from supabase import create_client
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    r = client.table("trades").select("id", count="exact")\
        .eq("user_id", user_id).eq("mode", "sim")\
        .not_.is_("exit_price", "null").execute()
    return r.count or 0


def _get_win_count(user_id: str) -> int:
    from supabase import create_client
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    r = client.table("trades").select("id", count="exact")\
        .eq("user_id", user_id).eq("mode", "sim")\
        .gt("pnl_usdt", 0).execute()
    return r.count or 0


def start_sim(user_id: str) -> bool:
    if user_id in _running:
        return False
    stop_event = threading.Event()
    _running[user_id] = stop_event
    t = threading.Thread(target=_sim_loop, args=(user_id, stop_event), daemon=True)
    t.start()
    return True


def stop_sim(user_id: str) -> bool:
    if user_id not in _running:
        return False
    _running[user_id].set()
    del _running[user_id]
    return True


def is_running(user_id: str) -> bool:
    return user_id in _running
