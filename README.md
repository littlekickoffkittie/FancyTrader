# FangBlenny-BOT

Advanced Phemex Perpetual Trading Bot with Dynamic Cooldowns, Adaptive Filtering, and a beautiful Web Dashboard.

## Features
- **Dynamic Cooldowns:** Multi-factor readiness score ($R$) based on time, volatility, order book imbalance, and price behavior.
- **Adaptive Filtering:** Entropy-based min-score adjustment to handle market saturation.
- **Dual Scanner:** Simultaneous LONG and SHORT scanning with Kelly-optimized position sizing.
- **Web Dashboard:** Real-time analytics and performance tracking.

## Usage

### 1. Setup
```bash
./setup.sh
```

### 2. Run Simulation Bot
```bash
python3 sim_bot.py
```

### 3. Launch Web Dashboard
```bash
python3 web_server.py
```
Then visit `http://localhost:8080` in your browser.

### 4. Backtesting
```bash
python3 backtest.py --timeframe 4H --candles 1000
```

## Dashboard Components
- **Equity Curve:** Visualizes live paper trading performance.
- **Top Signals:** Analysis of which technical signals are firing most frequently.
- **Symbol PnL:** Performance breakdown per traded asset.
- **Recent Trades:** live log of completed transactions.