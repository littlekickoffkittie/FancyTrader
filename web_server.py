#!/usr/bin/env python3
import http.server
import socketserver
import json
import os
import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PORT = 8080
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            data = self.get_dashboard_data()
            self.wfile.write(json.dumps(data).encode())
        else:
            # Serve index.html for root or handle standard file requests
            if self.path == '/' or self.path == '':
                self.path = '/index.html'
            return super().do_GET()

    def get_dashboard_data(self):
        data = {
            "metadata": {
                "botName": os.getenv("BOT_NAME", "FANGBLENNY"),
                "version": os.getenv("BOT_VERSION", "v2.2-LIVE"),
                "exchange": "PHEMEX",
                "market": f"{os.getenv('BOT_DIRECTION', 'MULTI')}-DIR {os.getenv('BOT_TIMEFRAME', '4H')}",
                "currency": "USDT",
                "currency_symbol": "$",
                "serverTime": datetime.datetime.now().isoformat(),
                "status": "OPERATIONAL"
            },
            "equityCurve": [],
            "symData": [],
            "topSignals": [],
            "recentTrades": [],
            "openPositions": [],
            "logs": [],
            "cooldowns": [],
            "metrics": {
                "balance": 0,
                "livePnL": 0,
                "winRate": 0,
                "wins": 0,
                "losses": 0,
                "avgHold": 0,
                "openCount": 0,
                "dailyPnL": 0,
                "dailyWins": 0,
                "dailyLosses": 0,
                "startEquity": 0
            },
            "backtest": {
                "pnl": 0,
                "winRate": 0,
                "wins": 0,
                "losses": 0,
                "pf": 0,
                "topSym": "...",
                "topPnl": 0,
                "worstSym": "...",
                "worstPnl": 0
            }
        }

        # 1. Paper Account (Balance & Positions)
        paper_file = Path(DIRECTORY) / "paper_account.json"
        if paper_file.exists():
            try:
                paper = json.loads(paper_file.read_text())
                data["metrics"]["balance"] = paper.get("balance", 0)
                data["openPositions"] = paper.get("positions", [])
                data["metrics"]["openCount"] = len(data["openPositions"])
            except: pass

        # 2. Sim Trade Results (Metrics)
        sim_results_file = Path(DIRECTORY) / "sim_trade_results.json"
        if sim_results_file.exists():
            try:
                trades = json.loads(sim_results_file.read_text())
                sorted_trades = sorted(trades, key=lambda x: x.get("timestamp", ""))
                
                wins = [t for t in trades if t.get("pnl", 0) > 0]
                losses = [t for t in trades if t.get("pnl", 0) <= 0]
                data["metrics"]["wins"] = len(wins)
                data["metrics"]["losses"] = len(losses)
                total = len(trades)
                
                if total > 0:
                    data["metrics"]["winRate"] = (len(wins) / total) * 100
                    data["metrics"]["livePnL"] = sum(t.get("pnl", 0) for t in trades)
                    data["metrics"]["avgHold"] = sum(t.get("hold_time_s", 0) for t in trades) / (total * 60) # minutes
                
                # Daily Stats
                today = datetime.datetime.now().date().isoformat()
                daily_trades = [t for t in trades if t.get("timestamp", "").startswith(today)]
                data["metrics"]["dailyPnL"] = sum(t.get("pnl", 0) for t in daily_trades)
                data["metrics"]["dailyWins"] = len([t for t in daily_trades if t.get("pnl", 0) > 0])
                data["metrics"]["dailyLosses"] = len([t for t in daily_trades if t.get("pnl", 0) <= 0])
                
                # Recent Trades (Show All)
                data["recentTrades"] = sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)
                
                # Equity Curve Construction
                current_balance = data["metrics"]["balance"]
                total_pnl = data["metrics"]["livePnL"]
                start_balance = current_balance - total_pnl
                data["metrics"]["startEquity"] = start_balance
                
                balance = start_balance
                curve = [balance]
                for t in sorted_trades:
                    balance += t.get("pnl", 0)
                    curve.append(balance)
                data["equityCurve"] = curve[-200:]
            except: 
                data["metrics"]["startEquity"] = 100.0
                data["equityCurve"] = [100.0]
        else:
            data["metrics"]["startEquity"] = data["metrics"]["balance"] or 100.0
            data["equityCurve"] = [data["metrics"]["startEquity"]]

        # 3. Logs (TUI-style)
        log_files = ["sim_bot.log", "bot.log"]
        for lf in log_files:
            log_path = Path(DIRECTORY) / lf
            if log_path.exists():
                try:
                    with open(log_path, "r") as f:
                        lines = f.readlines()
                        # Get last 20 lines, strip colors if any (though standard logs usually don't have them)
                        data["logs"] = [line.strip() for line in lines[-20:]]
                    break # Found one, good enough
                except: pass

        # 4. Cooldowns
        cd_files = [("sim_cooldowns.json", True), ("bot_blacklist.json", False)]
        for cf, is_sim in cd_files:
            cd_path = Path(DIRECTORY) / cf
            if cd_path.exists():
                try:
                    cds_raw = json.loads(cd_path.read_text())
                    now = datetime.datetime.now().timestamp()
                    
                    # Handle sim_cooldowns.json nested structure
                    if is_sim and isinstance(cds_raw, dict) and "last_exit" in cds_raw:
                        cds = cds_raw["last_exit"]
                    else:
                        cds = cds_raw
                        
                    for sym, val in cds.items():
                        # Handle both timestamp (float) and StopContext (dict)
                        timestamp = val.get("timestamp", 0) if isinstance(val, dict) else float(val)
                        stop_count = val.get("stop_count", 1) if isinstance(val, dict) else 1
                        
                        if isinstance(val, dict):
                             # Show context-based cooldowns (they don't strictly expire by time anymore)
                             data["cooldowns"].append({
                                 "symbol": sym, 
                                 "timestamp": timestamp, 
                                 "is_context": True,
                                 "stop_count": stop_count
                             })
                        elif timestamp > now:
                             data["cooldowns"].append({
                                 "symbol": sym, 
                                 "expiry": timestamp, 
                                 "is_context": False,
                                 "stop_count": 1
                             })
                    break
                except: pass

        # 3. Backtest Results
        bt_file = Path(DIRECTORY) / "backtest_results.json"
        if bt_file.exists():
            try:
                bt_data = json.loads(bt_file.read_text())
                # If it's a list of trades
                if isinstance(bt_data, list) and len(bt_data) > 0 and "pnl_usdt" in bt_data[0]:
                    trades = bt_data
                    wins = [t for t in trades if t.get("pnl_usdt", 0) > 0]
                    losses = [t for t in trades if t.get("pnl_usdt", 0) <= 0]
                    data["backtest"]["pnl"] = sum(t.get("pnl_usdt", 0) for t in trades)
                    data["backtest"]["wins"] = len(wins)
                    data["backtest"]["losses"] = len(losses)
                    if len(trades) > 0:
                        data["backtest"]["winRate"] = (len(wins) / len(trades)) * 100
                    
                    gross_profit = sum(t.get("pnl_usdt", 0) for t in wins)
                    gross_loss = abs(sum(t.get("pnl_usdt", 0) for t in losses))
                    data["backtest"]["pf"] = gross_profit / (gross_loss if gross_loss > 0 else 1)
                    
                    # Per-symbol PnL
                    sym_pnl = {}
                    for t in trades:
                        s = t.get("symbol", "???")
                        sym_pnl[s] = sym_pnl.get(s, 0) + t.get("pnl_usdt", 0)
                    
                    sorted_syms = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)
                    if sorted_syms:
                        data["backtest"]["topSym"] = sorted_syms[0][0]
                        data["backtest"]["topPnl"] = sorted_syms[0][1]
                        data["backtest"]["worstSym"] = sorted_syms[-1][0]
                        data["backtest"]["worstPnl"] = sorted_syms[-1][1]
                    
                    data["symData"] = [{"symbol": s, "pnl": p} for s, p in sorted_syms]
                    
                    # Signal Analysis
                    signal_counts = {}
                    for t in trades:
                        for sig in t.get("signals", []):
                            # Strip dynamic numbers from signals for grouping
                            clean_sig = sig.split(" (")[0].split(" [")[0]
                            signal_counts[clean_sig] = signal_counts.get(clean_sig, 0) + 1
                    
                    data["topSignals"] = sorted(signal_counts.items(), key=lambda x: x[1], reverse=True)[:12]

                # Else if it's a list of parameter configurations (from sweep)
                elif isinstance(bt_data, list) and len(bt_data) > 0 and "total_pnl" in bt_data[0]:
                    best = bt_data[0]
                    data["backtest"]["pnl"] = best.get("total_pnl", 0)
                    data["backtest"]["winRate"] = best.get("win_rate", 0)
                    data["backtest"]["pf"] = best.get("profit_factor", 0)
            except: pass

        return data

if __name__ == "__main__":
    os.chdir(DIRECTORY)
    url = f"http://localhost:{PORT}"
    
    # Try to open the browser automatically (specific to Termux/Android)
    print(f"Attempting to open dashboard at {url}...")
    os.system(f"termux-open-url {url} > /dev/null 2>&1")
    
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        print(f"Dashboard available at {url}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.server_close()
