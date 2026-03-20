import math
from dataclasses import dataclass, field
from typing import List, Optional
from src.core.config_schema import BotConfig


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    size_usdt: float
    leverage: int
    pnl_usdt: float
    pnl_pct: float
    entry_idx: int
    exit_idx: int
    exit_reason: str
    signal_score: float


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    starting_equity: float = 100.0
    final_equity: float = 100.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    realized_pnl: float = 0.0


def calc_atr(candles: List[Candle], period: int = 14) -> List[float]:
    atrs = [0.0] * len(candles)
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close)
        )
        if i < period:
            atrs[i] = tr
        else:
            atrs[i] = (atrs[i - 1] * (period - 1) + tr) / period
    return atrs


def calc_rsi(candles: List[Candle], period: int = 14) -> List[float]:
    rsis = [50.0] * len(candles)
    gains, losses = [], []
    for i in range(1, len(candles)):
        delta = candles[i].close - candles[i - 1].close
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
        if i >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                rsis[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsis[i] = 100 - (100 / (1 + rs))
    return rsis


def score_candle(i: int, candles: List[Candle], rsis: List[float], config: BotConfig) -> float:
    """Simple momentum score — replace with your full signal pipeline later."""
    score = 0.0
    c = candles[i]
    rsi = rsis[i]

    # Momentum component
    if i >= config.signal.momentum_lookback:
        lookback_close = candles[i - config.signal.momentum_lookback].close
        momentum = (c.close - lookback_close) / lookback_close * 100
        score += min(abs(momentum) * 10, 60)

    # RSI component
    if rsi < config.signal.rsi_oversold:
        score += 40
    elif rsi > config.signal.rsi_overbought:
        score += 40

    return score


def run_backtest(candles: List[Candle], config: BotConfig, starting_equity: float = 100.0) -> BacktestResult:
    result = BacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    result.equity_curve.append(equity)

    atrs = calc_atr(candles, 14)
    rsis = calc_rsi(candles, config.signal.rsi_period)

    open_trades: List[dict] = []
    gross_profit = 0.0
    gross_loss = 0.0
    peak_equity = equity

    for i in range(config.signal.momentum_lookback + 14, len(candles)):
        c = candles[i]

        # Check exits first
        still_open = []
        for t in open_trades:
            hit_sl = (t["direction"] == "long" and c.low <= t["sl"]) or \
                     (t["direction"] == "short" and c.high >= t["sl"])
            hit_tp = (t["direction"] == "long" and c.high >= t["tp"]) or \
                     (t["direction"] == "short" and c.low <= t["tp"])

            if hit_sl or hit_tp:
                exit_price = t["tp"] if hit_tp else t["sl"]
                exit_reason = "tp" if hit_tp else "sl"

                if t["direction"] == "long":
                    pnl_pct = (exit_price - t["entry"]) / t["entry"]
                else:
                    pnl_pct = (t["entry"] - exit_price) / t["entry"]

                pnl_usdt = pnl_pct * t["size_usdt"] * t["leverage"]
                equity += pnl_usdt

                trade = BacktestTrade(
                    symbol="SYMBOL",
                    direction=t["direction"],
                    entry_price=t["entry"],
                    exit_price=exit_price,
                    size_usdt=t["size_usdt"],
                    leverage=t["leverage"],
                    pnl_usdt=round(pnl_usdt, 4),
                    pnl_pct=round(pnl_pct * 100, 4),
                    entry_idx=t["entry_idx"],
                    exit_idx=i,
                    exit_reason=exit_reason,
                    signal_score=t["score"]
                )
                result.trades.append(trade)

                if pnl_usdt > 0:
                    result.wins += 1
                    gross_profit += pnl_usdt
                else:
                    result.losses += 1
                    gross_loss += abs(pnl_usdt)
            else:
                still_open.append(t)

        open_trades = still_open
        result.equity_curve.append(round(equity, 4))

        # Circuit breaker
        drawdown = (peak_equity - equity) / peak_equity * 100
        if drawdown >= config.risk.max_drawdown_pct:
            break

        peak_equity = max(peak_equity, equity)

        # Entry logic
        if len(open_trades) >= config.risk.max_positions:
            continue

        score = score_candle(i, candles, rsis, config)
        if score < config.signal.score_gate:
            continue

        atr = atrs[i]
        if atr == 0:
            continue

        rsi = rsis[i]
        direction = None
        if config.execution.allow_long and rsi < config.signal.rsi_oversold:
            direction = "long"
        elif config.execution.allow_short and rsi > config.signal.rsi_overbought:
            direction = "short"

        if not direction:
            continue

        sl_distance = atr * config.risk.stop_loss_atr_multiplier
        tp_distance = sl_distance * config.risk.take_profit_ratio

        if direction == "long":
            sl = c.close - sl_distance
            tp = c.close + tp_distance
        else:
            sl = c.close + sl_distance
            tp = c.close - tp_distance

        open_trades.append({
            "direction": direction,
            "entry": c.close,
            "sl": sl,
            "tp": tp,
            "size_usdt": config.risk.trade_size_usdt,
            "leverage": config.risk.leverage,
            "score": score,
            "entry_idx": i
        })

    # Final stats
    result.total_trades = len(result.trades)
    result.final_equity = round(equity, 4)
    result.realized_pnl = round(equity - starting_equity, 4)
    result.win_rate = round(result.wins / result.total_trades * 100, 2) if result.total_trades else 0.0
    result.profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0
    result.avg_pnl = round(result.realized_pnl / result.total_trades, 4) if result.total_trades else 0.0

    if result.trades:
        result.best_trade = max(t.pnl_usdt for t in result.trades)
        result.worst_trade = min(t.pnl_usdt for t in result.trades)

    if result.equity_curve:
        peak = result.equity_curve[0]
        for e in result.equity_curve:
            peak = max(peak, e)
            dd = (peak - e) / peak * 100
            result.max_drawdown_pct = max(result.max_drawdown_pct, dd)
        result.max_drawdown_pct = round(result.max_drawdown_pct, 2)

    return result
