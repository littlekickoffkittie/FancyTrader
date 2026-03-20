import os
from dotenv import load_dotenv
from supabase import create_client
from typing import List, Optional

load_dotenv()
_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def get_open_positions(user_id: str) -> List[dict]:
    result = _client.table("trades")\
        .select("*")\
        .eq("user_id", user_id)\
        .eq("mode", "sim")\
        .is_("exit_price", "null")\
        .execute()
    return result.data or []


def open_position(user_id: str, symbol: str, direction: str,
                  entry_price: float, size_usdt: float, leverage: int,
                  sl: float, tp: float, signal_score: float) -> dict:
    row = {
        "user_id": user_id,
        "mode": "sim",
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "size_usdt": size_usdt,
        "leverage": leverage,
        "signal_score": signal_score,
        "metadata": {"sl": sl, "tp": tp}
    }
    result = _client.table("trades").insert(row).execute()
    return result.data[0]


def close_position(trade_id: str, exit_price: float,
                   pnl_usdt: float, pnl_pct: float, exit_reason: str) -> None:
    from datetime import datetime, timezone
    _client.table("trades").update({
        "exit_price": exit_price,
        "pnl_usdt": round(pnl_usdt, 4),
        "pnl_pct": round(pnl_pct, 4),
        "exit_reason": exit_reason,
        "exit_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", trade_id).execute()


def log_event(user_id: str, level: str, message: str, payload: dict = {}) -> None:
    _client.table("execution_logs").insert({
        "user_id": user_id,
        "mode": "sim",
        "level": level,
        "message": message,
        "payload": payload
    }).execute()


def save_snapshot(user_id: str, equity: float, delta: float,
                  win_rate: float, profit_factor: float,
                  max_drawdown: float, total_trades: int) -> None:
    _client.table("performance_snapshots").insert({
        "user_id": user_id,
        "mode": "sim",
        "equity": round(equity, 4),
        "delta": round(delta, 4),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "total_trades": total_trades
    }).execute()
