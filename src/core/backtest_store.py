import os
from dotenv import load_dotenv
from supabase import create_client
from src.core.backtest_engine import BacktestResult, BacktestTrade

load_dotenv()
_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

def save_backtest_result(user_id: str, result: BacktestResult) -> None:
    # Save performance snapshot
    _client.table("performance_snapshots").insert({
        "user_id": user_id,
        "mode": "backtest",
        "equity": result.final_equity,
        "delta": result.realized_pnl,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "max_drawdown": result.max_drawdown_pct,
        "total_trades": result.total_trades
    }).execute()

    # Save individual trades
    if not result.trades:
        return

    rows = [
        {
            "user_id": user_id,
            "mode": "backtest",
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "size_usdt": t.size_usdt,
            "leverage": t.leverage,
            "pnl_usdt": t.pnl_usdt,
            "pnl_pct": t.pnl_pct,
            "exit_reason": t.exit_reason,
            "signal_score": t.signal_score,
            "metadata": {"entry_idx": t.entry_idx, "exit_idx": t.exit_idx}
        }
        for t in result.trades
    ]
    _client.table("trades").insert(rows).execute()
