import os
import time
import threading
from typing import Dict, Optional
from dotenv import load_dotenv
from supabase import create_client
from src.core.config_schema import BotConfig
from src.core.config_manager import load_config
from src.core.sim_state import log_event, save_snapshot
from src.core.live_orders import place_market_order, close_market_order
from src.core.key_validator import validate_phemex_key
from src.core.crypto import decrypt

load_dotenv()
_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

_running: Dict[str, threading.Event] = {}


def _get_mark_price(symbol: str) -> Optional[float]:
    import urllib.request, json
    url = f"https://api.phemex.com/md/v3/ticker/24hr?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        # Phemex V3 ticker response structure: markRp is the string we need
        rp = data.get("result", {}).get("markRp")
        if rp is not None:
            return float(rp)
    except Exception:
        pass
    return None


def _score_symbol(symbol: str, config: BotConfig) -> tuple:
    """Mirror of sim scoring — same function, same logic."""
    import random
    price = _get_mark_price(symbol)
    if not price:
        return 0.0, None, None, None
    score = random.uniform(40, 100)
    direction = "long" if random.random() > 0.5 else "short"
    atr = price * 0.005
    return score, direction, price, atr


def _get_credentials(user_id: str) -> Optional[tuple]:
    r = _client.table("api_credentials")\
        .select("*")\
        .eq("user_id", user_id)\
        .eq("permissions_validated", True)\
        .limit(1).execute()
    if not r.data:
        return None
    row = r.data[0]
    api_key = decrypt(row["encrypted_api_key"])
    api_secret = decrypt(row["encrypted_api_secret"])
    return api_key, api_secret


def _get_open_live_positions(user_id: str) -> list:
    r = _client.table("trades").select("*")\
        .eq("user_id", user_id)\
        .eq("mode", "live")\
        .is_("exit_price", "null").execute()
    return r.data or []


def _live_loop(user_id: str, stop_event: threading.Event, dry_run: bool) -> None:
    mode_label = "DRY-RUN" if dry_run else "LIVE"
    log_event(user_id, "audit", f"{mode_label} engine started")

    creds = _get_credentials(user_id)
    if not creds and not dry_run:
        log_event(user_id, "error", "No validated API credentials found")
        return

    starting_equity = 100.0
    equity = starting_equity
    gross_profit = 0.0
    gross_loss = 0.0
    peak_equity = equity

    while not stop_event.is_set():
        try:
            config = load_config(user_id)

            # --- Check open positions ---
            positions = _get_open_live_positions(user_id)
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

                    if not dry_run and creds:
                        close_side = "Sell" if direction == "long" else "Buy"
                        resp = close_market_order(creds[0], creds[1], symbol, close_side, size)
                        log_event(user_id, "audit", f"Close order response",
                                  {"response": resp, "symbol": symbol})

                    from datetime import datetime, timezone
                    _client.table("trades").update({
                        "exit_price": exit_price,
                        "pnl_usdt": round(pnl_usdt, 4),
                        "pnl_pct": round(pnl_pct * 100, 4),
                        "exit_reason": exit_reason,
                        "exit_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", pos["id"]).execute()

                    equity += pnl_usdt
                    log_event(user_id, "audit",
                              f"[{mode_label}] Closed {direction} {symbol} via {exit_reason}",
                              {"pnl_usdt": round(pnl_usdt, 4)})

                    if pnl_usdt > 0:
                        gross_profit += pnl_usdt
                    else:
                        gross_loss += abs(pnl_usdt)

            # --- Circuit breaker ---
            drawdown = (peak_equity - equity) / peak_equity * 100
            if drawdown >= config.risk.max_drawdown_pct:
                log_event(user_id, "audit",
                          f"[{mode_label}] Circuit breaker at {drawdown:.1f}%")
                stop_event.set()
                break

            peak_equity = max(peak_equity, equity)

            # --- Entry scan ---
            positions = _get_open_live_positions(user_id)
            if len(positions) < config.risk.max_positions:
                candidates = config.scanner.symbol_whitelist or \
                             ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
                open_symbols = {p["symbol"] for p in positions}

                for symbol in candidates:
                    if symbol in config.scanner.symbol_blacklist:
                        continue
                    if symbol in open_symbols:
                        continue

                    score, direction, price, atr = _score_symbol(symbol, config)
                    if score < config.signal.score_gate:
                        continue
                    if not direction or not price:
                        continue
                    if not config.execution.allow_long and direction == "long":
                        continue
                    if not config.execution.allow_short and direction == "short":
                        continue

                    sl_dist = atr * config.risk.stop_loss_atr_multiplier
                    tp_dist = sl_dist * config.risk.take_profit_ratio
                    sl = (price - sl_dist) if direction == "long" else (price + sl_dist)
                    tp = (price + tp_dist) if direction == "long" else (price - tp_dist)

                    if not dry_run and creds:
                        side = "Buy" if direction == "long" else "Sell"
                        resp = place_market_order(
                            creds[0], creds[1], symbol, side,
                            config.risk.trade_size_usdt, config.risk.leverage
                        )
                        log_event(user_id, "audit", f"Open order response",
                                  {"response": resp, "symbol": symbol})

                    # Record to DB regardless of dry_run
                    _client.table("trades").insert({
                        "user_id": user_id,
                        "mode": "live",
                        "symbol": symbol,
                        "direction": direction,
                        "entry_price": price,
                        "size_usdt": config.risk.trade_size_usdt,
                        "leverage": config.risk.leverage,
                        "signal_score": score,
                        "metadata": {"sl": sl, "tp": tp, "dry_run": dry_run}
                    }).execute()

                    log_event(user_id, "audit",
                              f"[{mode_label}] Opened {direction} {symbol} @ {price}",
                              {"score": score, "sl": sl, "tp": tp})

                    open_symbols.add(symbol)
                    if len(open_symbols) >= config.risk.max_positions:
                        break

        except Exception as e:
            log_event(user_id, "error", f"[{mode_label}] Loop error: {e}")

        stop_event.wait(config.scanner.scan_interval_seconds)

    log_event(user_id, "audit", f"[{mode_label}] Engine stopped")


def start_live(user_id: str, dry_run: bool = True) -> bool:
    if user_id in _running:
        return False
    stop_event = threading.Event()
    _running[user_id] = stop_event
    t = threading.Thread(
        target=_live_loop,
        args=(user_id, stop_event, dry_run),
        daemon=True
    )
    t.start()
    return True


def stop_live(user_id: str) -> bool:
    if user_id not in _running:
        return False
    _running[user_id].set()
    del _running[user_id]
    return True


def is_running(user_id: str) -> bool:
    return user_id in _running
