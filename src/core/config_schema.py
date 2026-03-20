from dataclasses import dataclass, field, asdict
from typing import List

@dataclass
class SignalConfig:
    score_gate: float = 60.0
    score_gap_min: float = 30.0
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    hawkes_decay: float = 0.1
    hawkes_intensity_threshold: float = 0.5
    entropy_sensitivity: float = 0.3
    momentum_lookback: int = 20

@dataclass
class RiskConfig:
    kelly_fraction: float = 0.5
    max_positions: int = 5
    trade_size_usdt: float = 10.0
    leverage: int = 20
    stop_loss_atr_multiplier: float = 1.5
    take_profit_ratio: float = 2.0
    max_drawdown_pct: float = 20.0
    use_fixed_stop: bool = False
    fixed_stop_pct: float = 5.0

@dataclass
class ExecutionConfig:
    allow_long: bool = True
    allow_short: bool = True
    cooldown_seconds: int = 86400
    time_filter_enabled: bool = False
    time_filter_utc_start: int = 0
    time_filter_utc_end: int = 23
    funding_rate_filter: float = 0.01
    dry_run: bool = True

@dataclass
class ScannerConfig:
    scan_interval_seconds: int = 30
    min_volume_usdt: float = 500000.0
    max_spread_pct: float = 0.1
    symbol_whitelist: List[str] = field(default_factory=list)
    symbol_blacklist: List[str] = field(default_factory=list)

@dataclass
class BotConfig:
    mode: str = "backtest"
    signal: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        return cls(
            mode=data.get("mode", "backtest"),
            signal=SignalConfig(**data.get("signal", {})),
            risk=RiskConfig(**data.get("risk", {})),
            execution=ExecutionConfig(**data.get("execution", {})),
            scanner=ScannerConfig(**data.get("scanner", {})),
        )