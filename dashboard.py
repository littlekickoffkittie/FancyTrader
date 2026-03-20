#!/usr/bin/env python3
"""
FancyBot Dashboard System
─────────────────────────
Advanced TUI components for building sophisticated terminal dashboards.
Inspired by blessed-contrib (Node.js) but implemented in Python using blessed.
"""

import datetime
import math
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple, Union

import blessed
from colorama import Fore, Style

# ── Braille Constants ─────────────────────────────────────────────────────────
# 2x4 dot matrix per character
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

# ── UI Primitives ─────────────────────────────────────────────────────────────

def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1B(?:[@-Z\-_]|\[[0-?]*[ -/]*[@-~])', '', text)

def rpad(text: str, width: int) -> str:
    """Pad string with spaces to width, handling ANSI escape sequences."""
    plain_text = _strip_ansi(text)
    padding = max(0, width - len(plain_text))
    return text + (" " * padding)

def lpad(text: str, width: int) -> str:
    """Left-pad string with spaces to width, handling ANSI escape sequences."""
    plain_text = _strip_ansi(text)
    padding = max(0, width - len(plain_text))
    return (" " * padding) + text

# ── Dashboard Components ──────────────────────────────────────────────────────

class Grid:
    """Layout helper to split terminal into a grid."""
    def __init__(self, term: blessed.Terminal, rows: int = 12, cols: int = 12):
        self.term = term
        self.rows = rows
        self.cols = cols
        self.update_dimensions()

    def update_dimensions(self):
        self.width = self.term.width
        self.height = self.term.height

    def get_coords(self, row: int, col: int, rowspan: int = 1, colspan: int = 1) -> Tuple[int, int, int, int]:
        """Returns (x, y, w, h) for a grid cell."""
        cell_w = self.width // self.cols
        cell_h = self.height // self.rows
        x = col * cell_w
        y = row * cell_h
        w = colspan * cell_w
        h = rowspan * cell_h
        return x, y, w, h
    
    def place(self, component: Any, row: int, col: int, rowspan: int = 1, colspan: int = 1, **kwargs):
        """Convenience method to draw a component at a grid location."""
        x, y, w, h = self.get_coords(row, col, rowspan, colspan)
        component.draw(self.term, x, y, w, h, **kwargs)

class Box:
    """A bordered container with an optional title and custom border styles."""
    STYLES = {
        "thin":   ("┌", "─", "┐", "│", "└", "─", "┘"),
        "heavy":  ("┏", "━", "┓", "┃", "┗", "━", "┛"),
        "double": ("╔", "═", "╗", "║", "╚", "═", "╝"),
        "rounded":("╭", "─", "╮", "│", "╰", "─", "╯"),
    }

    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, title: str = "", color: Any = None, style: str = "thin"):
        if color is None: color = term.cyan
        chars = Box.STYLES.get(style, Box.STYLES["thin"])
        tl, top, tr, side, bl, bot, br = chars
        
        # Draw borders
        print(term.move_xy(x, y) + color(tl + top * (w - 2) + tr))
        for i in range(1, h - 1):
            print(term.move_xy(x, y + i) + color(side) + " " * (w - 2) + color(side))
        print(term.move_xy(x, y + h - 1) + color(bl + bot * (w - 2) + br))
        
        # Draw title
        if title:
            title_text = f" {title} "
            if len(title_text) > w - 4:
                title_text = title_text[:w-7] + "... "
            print(term.move_xy(x + 2, y) + term.bold(title_text))

class BigStat:
    """Displays a large, centered statistic with a label."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, label: str, value: str, color: Any = None, subtext: str = ""):
        if color is None: color = term.white
        # Use a lambda to compose dim + cyan since blessed doesn't support dim_cyan as a property
        Box.draw(term, x, y, w, h, "", color=lambda s: term.dim + term.cyan + s + term.normal, style="rounded")
        
        mid_y = y + (h // 2)
        val_str = term.bold(value)
        
        # Center the label
        label_x = x + (w - len(label)) // 2
        print(term.move_xy(label_x, y + 1) + term.dim(label))
        
        # Center the value
        plain_val = _strip_ansi(value)
        val_x = x + (w - len(plain_val)) // 2
        print(term.move_xy(val_x, mid_y) + color(val_str))
        
        if subtext:
            sub_x = x + (w - len(subtext)) // 2
            print(term.move_xy(sub_x, mid_y + 1) + term.dim(subtext))

class LineChart:
    """Braille-based line chart."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, data: List[float], title: str = "", color: Any = None):
        if not data: data = [0.0]
        if color is None: color = term.green
        
        inner_w = w - 4
        inner_h = h - 4
        points = data[-(inner_w * 2):]
        while len(points) < inner_w * 2:
            points = [points[0]] * (inner_w * 2 - len(points)) + points
            
        lo, hi = min(points), max(points)
        span = (hi - lo) or 1e-10
        rows = inner_h * 4
        
        def to_row(v):
            return int((v - lo) / span * (rows - 1))
            
        scaled = [to_row(p) for p in points]
        grid = [[[0, 0] for _ in range(inner_w)] for _ in range(inner_h)]
        
        for col_idx in range(inner_w):
            l_val = scaled[col_idx * 2]
            r_val = scaled[col_idx * 2 + 1]
            for val, side in [(l_val, 0), (r_val, 1)]:
                char_row = inner_h - 1 - (val // 4)
                dot_row = val % 4
                char_row = max(0, min(inner_h - 1, char_row))
                grid[char_row][col_idx][side] |= (1 << dot_row)
                
        # Draw Box
        Box.draw(term, x, y, w, h, title)
        
        # Draw Chart
        for r in range(inner_h):
            line = "".join(_to_braille(grid[r][c][0], grid[r][c][1]) for c in range(inner_w))
            print(term.move_xy(x + 2, y + 1 + r) + color(line))
            
        # Draw Stats
        stats = f"Min: {lo:.2f} Max: {hi:.2f} Cur: {data[-1]:.2f}"
        print(term.move_xy(x + 2, y + h - 2) + term.small(stats[:w-4]))

class Gauge:
    """Horizontal gauge/progress bar."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, percent: float, title: str = "", color: Any = None):
        if color is None:
            if percent >= 80: color = term.green
            elif percent >= 50: color = term.yellow
            else: color = term.red
            
        Box.draw(term, x, y, w, h, title)
        
        inner_w = w - 4
        filled = int((max(0, min(100, percent)) / 100.0) * inner_w)
        bar = "█" * filled + "░" * (inner_w - filled)
        
        # Center the bar vertically in the box
        bar_y = y + (h // 2)
        print(term.move_xy(x + 2, bar_y) + color(bar))
        print(term.move_xy(x + (w // 2) - 2, bar_y + 1) + term.bold(f"{percent:3.1f}%"))

class Table:
    """Simple table with headers."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, headers: List[str], rows: List[List[str]], title: str = ""):
        Box.draw(term, x, y, w, h, title)
        
        if not headers: return
        
        num_cols = len(headers)
        col_w = (w - 4) // num_cols
        
        # Draw Headers
        header_line = "".join(rpad(term.bold(h), col_w) for h in headers)
        print(term.move_xy(x + 2, y + 1) + header_line)
        print(term.move_xy(x + 2, y + 2) + term.cyan("─" * (w - 4)))
        
        # Draw Rows
        max_rows = h - 4
        for i, row in enumerate(rows[:max_rows]):
            row_line = "".join(rpad(str(cell), col_w) for cell in row)
            print(term.move_xy(x + 2, y + 3 + i) + row_line)

class ActivityFeed:
    """A more stylized scrolling log panel."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, logs: deque, title: str = "ACTIVITY"):
        Box.draw(term, x, y, w, h, title, style="rounded")
        max_logs = h - 2
        display_logs = list(logs)[-max_logs:]
        
        for i, log in enumerate(display_logs):
            time_str = datetime.datetime.now().strftime("%H:%M:%S")
            icon = "⚡"
            if "error" in log.lower(): icon = term.red("✖")
            elif "enter" in log.lower() or "long" in log.lower(): icon = term.green("↑")
            elif "exit" in log.lower() or "short" in log.lower(): icon = term.red("↓")
            
            line = f"{term.dim(time_str)} {icon} {log}"
            truncated = rpad(line, w - 4)[:w + 100] 
            print(term.move_xy(x + 2, y + 1 + i) + truncated)

class Heatmap:
    """Shows status of multiple assets or scanners in a grid."""
    @staticmethod
    def draw(term: blessed.Terminal, x: int, y: int, w: int, h: int, data: Dict[str, float], title: str = "SCANNERS"):
        Box.draw(term, x, y, w, h, title)
        inner_w = w - 4
        inner_h = h - 2
        
        items = list(data.items())
        if not items: return
        
        cols = max(1, inner_w // 15)
        rows = (len(items) + cols - 1) // cols
        
        for idx, (name, val) in enumerate(items):
            if idx >= inner_h * cols: break
            r = idx // cols
            c = idx % cols
            
            # Color based on value
            if val >= 140:   color = term.on_bright_green + term.black
            elif val >= 120: color = term.on_green + term.black
            elif val >= 100: color = term.on_yellow + term.black
            elif val >= 80:  color = term.on_bright_red + term.black
            else:            color = term.on_red + term.black
            
            block = f" {name[:8]:<8} {val:>3.0f} "
            print(term.move_xy(x + 2 + c * 15, y + 1 + r) + color + block + term.normal)

class Sparkline:
    """Mini unicode sparkline."""
    _CHARS = " ▂▃▄▅▆▇█"
    @staticmethod
    def get(data: List[float], width: int) -> str:
        if not data: return " " * width
        data = data[-width:]
        lo, hi = min(data), max(data)
        span = (hi - lo) or 1.0
        return "".join(Sparkline._CHARS[min(7, int((v - lo) / span * 8))] for v in data)

# ── Dashboard Layouts ─────────────────────────────────────────────────────────

class Theme:
    """Pre-defined color palettes."""
    def __init__(self, term: blessed.Terminal):
        self.primary = term.cyan
        self.secondary = term.bright_cyan
        self.success = term.green
        self.warning = term.yellow
        self.danger = term.red
        self.dim = term.dim
        self.bold = term.bold
        self.normal = term.normal
        self.bg_accent = term.on_bright_black
        # Use a lambda for dim + white as compound properties aren't supported directly
        self.stat_label = lambda s: term.dim + term.white + s + term.normal
        self.header = term.bold_bright_cyan

def draw_live_dashboard(term: blessed.Terminal, state: Dict[str, Any]):
    """Renders the full live bot dashboard with advanced layout and theming."""
    theme = Theme(term)
    grid = Grid(term, rows=24, cols=24)
    grid.update_dimensions()
    
    # Helper function to render the SL/TP bar
    def _render_sl_tp_bar(term: blessed.Terminal, p: Dict[str, Any]) -> str:
        bar_str = term.cyan("[") + term.white("──────────") + term.cyan("]")
        if p.get('stop_price') and p.get('take_profit'):
            lo = min(p['stop_price'], p['take_profit'], p['entry'], p['price'])
            hi = max(p['stop_price'], p['take_profit'], p['entry'], p['price'])
            span = hi - lo or 1.0
            def gp(v): return max(0, min(9, int((v - lo) / span * 9)))
            
            bar_chars = list("──────────")
            try:
                # Create a list of styled characters for the bar
                styled_bar_chars = [term.normal("─") for _ in range(10)]
                styled_bar_chars[gp(p['stop_price'])] = term.red("S")
                styled_bar_chars[gp(p['entry'])]      = term.yellow("E")
                styled_bar_chars[gp(p['take_profit'])] = term.green("T")
                
                # Replace dot with actual pnl number (current price indicator)
                pnl_val = p.get('pnl', 0.0)
                pnl_label = f"{pnl_val:+.2f}"
                pnl_color = term.bold_green if pnl_val >= 0 else term.bold_red
                pnl_styled = pnl_color(pnl_label)
                
                # Calculate insertion point
                pos = gp(p['price'])
                # Adjust to fit label if near edge
                if pos + len(pnl_label) > 10:
                    pos = 10 - len(pnl_label)
                
                # Insert the styled PnL string
                for i, char in enumerate(pnl_styled):
                    if pos + i < 10:
                        styled_bar_chars[pos + i] = char
                
                bar_str = term.cyan("[") + "".join(styled_bar_chars) + term.cyan("]")
            except: 
                bar_str = term.cyan("[") + "".join(str(c) for c in bar_chars) + term.cyan("]")
        return bar_str

    # ── 1. Header (Top 2/24) ──
    header_x, header_y, header_w, header_h = grid.get_coords(0, 0, rowspan=2, colspan=24)
    now = datetime.datetime.now().strftime("%H:%M:%S")
    title = f" ⚡ FANCYBOT LIVE PRODUCTION DASHBOARD | {now}"
    print(term.move_xy(header_x + 2, header_y + 1) + theme.header(title))
    print(term.move_xy(header_x, header_y + 2) + theme.primary("═" * term.width))
    
    # ── 2. Top Stats ──
    grid.place(BigStat, 2, 0, rowspan=4, colspan=4, label="EQUITY", value=f"${state.get('equity', 0.0):,.2f}", color=term.bright_cyan)
    
    upnl = state.get("upnl", 0.0)
    upnl_color = term.bright_green if upnl >= 0 else term.bright_red
    grid.place(BigStat, 2, 4, rowspan=4, colspan=4, label="UNREALIZED", value=f"{upnl:+.4f}", color=upnl_color)
    
    rpnl = sum(h.get("pnl", 0) for h in state.get("history", []))
    rpnl_color = term.bright_green if rpnl >= 0 else term.bright_red
    grid.place(BigStat, 2, 8, rowspan=4, colspan=4, label="REALIZED", value=f"{rpnl:+.4f}", color=rpnl_color)
    
    # ── 3. Gauges & Charts ──
    max_pos = state.get("max_positions", 5)
    usage_pct = (len(state.get("positions", [])) / max_pos) * 100 if max_pos > 0 else 0
    grid.place(Gauge, 6, 0, rowspan=4, colspan=6, percent=usage_pct, title="SLOTS USED")
    
    grid.place(LineChart, 2, 12, rowspan=8, colspan=12, data=state.get("equity_history", []), title="EQUITY PERFORMANCE")
    
    # ── 4. Position Table ──
    pos_x, pos_y, pos_w, pos_h = grid.get_coords(10, 0, rowspan=8, colspan=24)
    headers = ["SYMBOL", "SIDE", "ENTRY", "NOW", "PNL", "SL/TP BAR", "PNL HISTORY", "STOP DIST", "DURATION", "LEVERAGE"]
    rows = []
    positions = state.get("positions", [])
    for p in positions:
        side_color = term.green if p['side'].upper() in ['BUY', 'LONG'] else term.red
        pnl_color = term.green if p['pnl'] >= 0 else term.red
        
        # Duration calculation
        dur_str = "???"
        if p.get('entry_time'):
            try:
                et = datetime.datetime.fromisoformat(p['entry_time'])
                diff = datetime.datetime.now() - et
                tot_sec = int(diff.total_seconds())
                if tot_sec < 60: dur_str = f"{tot_sec}s"
                elif tot_sec < 3600: dur_str = f"{tot_sec//60}m"
                else: dur_str = f"{tot_sec//3600}h {(tot_sec%3600)//60}m"
            except: pass
        elif p.get('age'):
            dur_str = p['age']

        bar_str = _render_sl_tp_bar(term, p)
            
        trend = Sparkline.get(p.get('pnl_history', []), 10)
        rows.append([
            term.bold(p['symbol']),
            side_color(p['side']),
            f"{p['entry']:.5g}",
            f"{p['price']:.5g}",
            pnl_color(f"{p['pnl']:+.4f}"),
            bar_str,
            pnl_color(trend),
            f"{p.get('stop_dist', 0):.2f}%",
            dur_str,
            f"{p.get('leverage', '??')}x"
        ])
    Table.draw(term, pos_x, pos_y, pos_w, pos_h, headers, rows, f"ACTIVE POSITIONS ({len(positions)})")
    
    # ── 5. Lower Panels ──
    scanner_data = state.get("scanner_scores", {})
    if not scanner_data:
        scanner_data = {"BTC": 120, "ETH": 95, "SOL": 150, "BNB": 40, "XRP": 110, "ADA": 85}
        
    grid.place(Heatmap, 18, 0, rowspan=6, colspan=8, data=scanner_data)
    grid.place(ActivityFeed, 18, 8, rowspan=6, colspan=16, logs=state.get("logs", deque()))

if __name__ == "__main__":
    # Test
    term = blessed.Terminal()
    state = {
        "balance": 1000.0,
        "equity": 1050.0,
        "upnl": 50.0,
        "equity_history": [1000, 1010, 1005, 1020, 1030, 1025, 1050],
        "positions": [
            {"symbol": "BTCUSDT", "side": "Buy", "entry": 50000, "price": 51000, "pnl": 20.0, "stop_dist": 2.5, "pnl_history": [0, 5, 10, 20]},
            {"symbol": "ETHUSDT", "side": "Sell", "entry": 3000, "price": 2900, "pnl": 30.0, "stop_dist": 1.5, "pnl_history": [0, -5, 15, 30]}
        ],
        "history": [
            {"timestamp": "2024-03-06 12:00:00", "symbol": "SOLUSDT", "pnl": 15.0},
            {"timestamp": "2024-03-06 12:05:00", "symbol": "BNBUSDT", "pnl": -5.0}
        ],
        "logs": deque(["Bot started", "Scanning assets...", "Found BTCUSDT setup", "Entering Long BTCUSDT"], maxlen=10)
    }
    with term.fullscreen(), term.hidden_cursor():
        draw_live_dashboard(term, state)
        term.inkey(timeout=5)