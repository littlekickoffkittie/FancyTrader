#!/usr/bin/env python3
"""
FangBleeny UI Kit
─────────────────
Shared terminal display primitives used across all FangBleeny scripts.
Import with:  import ui
"""

from colorama import Fore, Style

W = 96  # standard terminal width

# ── Horizontal rules ──────────────────────────────────────────────────────────
def hr_double(color=Fore.CYAN): return color + "═" * W + Style.RESET_ALL
def hr_thin(color=Fore.CYAN):   return color + "─" * W + Style.RESET_ALL
def hr_dash(color=""):          return color + "┄" * W + Style.RESET_ALL
def hr_heavy():                 return Fore.WHITE + Style.BRIGHT + "━" * W + Style.RESET_ALL

# ── Score gauge ───────────────────────────────────────────────────────────────
def score_gauge(score: int, width: int = 24) -> str:
    """Visual bar gauge scaled 0–200."""
    clamped = max(0, min(score, 200))
    filled  = int(clamped / 200 * width)
    empty   = width - filled
    if score >= 145:   color = Fore.LIGHTGREEN_EX
    elif score >= 120: color = Fore.GREEN
    elif score >= 100: color = Fore.YELLOW
    elif score >= 80:  color = Fore.LIGHTYELLOW_EX
    else:              color = Fore.RED
    return f"{color}{'█' * filled}{'░' * empty}{Style.RESET_ALL}"

# ── Mini sparkline (equity curve etc.) ───────────────────────────────────────
_SPARK = "▁▂▃▄▅▆▇█"
def sparkline(values, width: int = 16) -> str:
    if not values: return "─" * width
    lo, hi = min(values), max(values)
    span   = hi - lo or 1
    idxs   = [min(7, int((v - lo) / span * 8)) for v in values[-width:]]
    return "".join(_SPARK[i] for i in idxs)

# ── Grade badge ───────────────────────────────────────────────────────────────
_GRADE_COLORS = {
    "A": Fore.LIGHTGREEN_EX,
    "B": Fore.GREEN,
    "C": Fore.YELLOW,
    "D": Fore.RED,
}
def grade_badge(score: int) -> str:
    """Return a coloured ▐G▌ grade badge string."""
    from phemex_common import grade
    g, _ = grade(score)
    c = _GRADE_COLORS.get(g, Fore.WHITE)
    return f"{c}▐{g}▌{Style.RESET_ALL}"

# ── Section header ────────────────────────────────────────────────────────────
def section(title: str, color=Fore.CYAN, char="═") -> str:
    pad   = f"  {title}  "
    side  = (W - len(pad)) // 2
    line  = char * side + pad + char * (W - side - len(pad))
    return color + Style.BRIGHT + line + Style.RESET_ALL

def section_left(title: str, color=Fore.CYAN) -> str:
    line = f"  {title}  " + "─" * max(0, W - len(title) - 4)
    return color + Style.BRIGHT + line + Style.RESET_ALL

# ── Coloured stat value ───────────────────────────────────────────────────────
def pnl_color(val: float) -> str:
    return Fore.LIGHTGREEN_EX if val > 0 else (Fore.RED if val < 0 else Fore.WHITE)

def colored(val, fmt="+.4f", pos_color=Fore.LIGHTGREEN_EX, neg_color=Fore.RED) -> str:
    c = pos_color if float(val) >= 0 else neg_color
    return f"{c}{val:{fmt}}{Style.RESET_ALL}"

# ── Direction label ───────────────────────────────────────────────────────────
def dir_label(direction: str) -> str:
    if direction == "LONG":
        return f"{Fore.LIGHTGREEN_EX}▲ LONG{Style.RESET_ALL}"
    return f"{Fore.RED}▼ SHORT{Style.RESET_ALL}"

# ── Win-rate bar ──────────────────────────────────────────────────────────────
def wr_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    color  = Fore.LIGHTGREEN_EX if pct >= 55 else (Fore.YELLOW if pct >= 45 else Fore.RED)
    return f"{color}{'█' * filled}{'░' * (width - filled)}{Style.RESET_ALL}"

# ── Box drawing helpers ───────────────────────────────────────────────────────
def box_top(w=W):    return Fore.CYAN + "┌" + "─" * (w - 2) + "┐" + Style.RESET_ALL
def box_mid(w=W):    return Fore.CYAN + "├" + "─" * (w - 2) + "┤" + Style.RESET_ALL
def box_bot(w=W):    return Fore.CYAN + "└" + "─" * (w - 2) + "┘" + Style.RESET_ALL
def box_row(text, w=W):
    inner = w - 4
    return Fore.CYAN + "│ " + Style.RESET_ALL + f"{text:<{inner}}" + Fore.CYAN + " │" + Style.RESET_ALL

