from dataclasses import dataclass, field
from typing import Optional
import time
import pandas as pd

TIMEFRAMES = {"SCALPING": ("15m", "5m"), "INTRADAY": ("4h", "15m"), "SWING": ("1d", "4h")}
MONITOR_TIMEFRAMES = ("1m", "5m", "15m")
INTERVAL_MS = {
    "1m": 60000,
    "5m": 300000,
    "15m": 900000,
    "1h": 3600000,
    "4h": 14400000,
    "1d": 86400000,
}
SPOT_SIMULATION_CHECKS = frozenset({"Deviasi Mark–Last", "Funding kontrak", "Kontrak linear USDT perpetual"})
DYNAMIC_LEVERAGE_OPTIONS = (2, 3, 5, 10, 15, 20)


@dataclass
class IntrabarCandle:
    """Single candle observation with explicit closed vs intrabar metadata (v6.0.5 Stage 1)."""
    symbol: str
    timeframe: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    provider: str = "Binance Futures"
    instrument_kind: str = "USD_M_PERPETUAL"
    exchange_timestamp_ms: int = 0
    local_receive_ms: int = 0
    data_freshness_ms: int = 0
    is_stale: bool = False
    failover_reason: Optional[str] = None

    def is_valid_ohlc(self) -> bool:
        vals = (self.open, self.high, self.low, self.close)
        if any(v is None or v <= 0 for v in vals):
            return False
        if self.volume is None or self.volume < 0:
            return False
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            return False
        if self.close_time <= self.open_time:
            return False
        return True


@dataclass
class MonitorSnapshot:
    """M1/M5/M15 live & closed market snapshot for Predictive Watchlist (v6.0.5 Stage 1).
    Strictly separates closed candles from running intrabar candles and never aliases Spot Last as Futures Mark Price.
    """
    symbol: str
    provider: str
    instrument_kind: str
    exchange_timestamp_ms: int
    local_receive_ms: int
    last_price: float
    mark_price: Optional[float]
    index_price: Optional[float]
    bid: float
    ask: float
    frames_closed: dict = field(default_factory=dict)
    frames_live: dict = field(default_factory=dict)
    current_candles: dict = field(default_factory=dict)
    data_freshness_ms: int = 0
    is_stale: bool = False
    failover_reason: Optional[str] = None
    transport: str = "REST"

    def __post_init__(self):
        if self.instrument_kind == "SPOT_REFERENCE":
            self.mark_price = None
            self.index_price = None

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2.0 if (self.bid > 0 and self.ask > 0) else 0.0
        if mid <= 0:
            return 0.0
        return abs(self.ask - self.bid) / mid * 100.0

    @property
    def reference_price(self) -> float:
        if self.instrument_kind == "SPOT_REFERENCE":
            return float(self.last_price)
        return float(self.mark_price if self.mark_price is not None else self.last_price)


@dataclass
class Contract:
    tick: float
    step: float
    min_qty: float
    max_qty: float
    min_notional: float
    market_step: float
    market_min_qty: float
    market_max_qty: float
    min_price: float = 0.0
    max_price: float = float("inf")
    multiplier_down: float = 0.0
    multiplier_up: float = float("inf")
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"
    quote_asset: str = "USDT"
    contract_multiplier: float = 1.0
    price_precision: int = 8
    quantity_precision: int = 8


@dataclass
class Snapshot:
    symbol: str
    mode: str
    now_ms: int
    bias: pd.DataFrame
    execution: pd.DataFrame
    contract: Contract
    last: float
    mark: Optional[float]
    bid: float
    ask: float
    quote_times: dict
    bids: list
    asks: list
    previous_day_high: Optional[float] = None
    previous_day_low: Optional[float] = None
    funding_rate: Optional[float] = None
    next_funding_ms: Optional[int] = None
    funding_interval_hours: Optional[float] = None
    open_bar_high: Optional[float] = None
    open_bar_low: Optional[float] = None
    open_mark_high: Optional[float] = None
    open_mark_low: Optional[float] = None
    source: str = "Binance USD-M Futures public REST"
    warnings: list = field(default_factory=list)
    instrument_kind: str = "USD_M_PERPETUAL"
    quote_time_basis: str = "EXCHANGE_TIMESTAMP"
    index_price: Optional[float] = None
    open_interest: Optional[float] = None
    data_timestamp: Optional[int] = None
    received_timestamp: Optional[int] = None
    market_provider: str = "Binance Futures"
    failover_reason: Optional[str] = None

    def __post_init__(self):
        if self.data_timestamp is None:
            self.data_timestamp = int(self.now_ms)
        if self.received_timestamp is None:
            self.received_timestamp = int(self.now_ms or int(time.time() * 1000))
        if self.instrument_kind == "SPOT_REFERENCE":
            self.mark = None
            self.index_price = None
            self.funding_rate = None
            self.open_interest = None

    @property
    def reference_price(self):
        """Spot PAPER uses Last; it does not manufacture a futures Mark price."""
        return self.last if self.instrument_kind == "SPOT_REFERENCE" else self.mark

    @property
    def quote_keys(self):
        return ("last", "book", "depth") if self.instrument_kind == "SPOT_REFERENCE" else ("last", "mark", "book", "depth")


@dataclass
class Account:
    equity: float = 1000.0
    free_margin: float = 1000.0
    day_start_equity: float = 1000.0
    net_cash_flow: float = 0.0
    account_day_utc: str = ""
    risk_pct: float = 0.5
    leverage: int = 2
    max_operator_leverage: int = 3
    confidence_calibrated: bool = False
    active_pending_count: int = 0
    total_existing_risk: float = 0.0
    correlated_alt_risk: float = 0.0
    consecutive_losses: int = 0
    isolated: bool = True
    auto_add_margin: bool = False
    account_verified: bool = False
    taker_fee: Optional[float] = None
    exit_slippage_bps: Optional[float] = None
    funding_reserve_rate: Optional[float] = None
    costs_verified: bool = False
    news_verified_at_ms: Optional[int] = None
    news_source: str = ""
    news_clear_from_ms: Optional[int] = None
    news_clear_until_ms: Optional[int] = None
    order_semantics_verified: bool = False
    liquidation_price: Optional[float] = None
    liquidation_plan_id: str = ""
    liquidation_source: str = ""
    liquidation_checked_ms: Optional[int] = None


@dataclass
class Result:
    status: str = "WAIT"
    direction: Optional[str] = None
    checks: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    candidate: Optional[dict] = None
    plan: Optional[dict] = None
    metrics: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def check(self, name, actual, threshold, ok=None, missing=False):
        state = "TIDAK TERSEDIA" if missing else ("LULUS" if ok else "GAGAL")
        self.checks.append({"Kriteria": name, "Nilai aktual": str(actual), "Syarat": str(threshold), "Status": state})

    def finish(self):
        missing = [x["Kriteria"] for x in self.checks if x["Status"] == "TIDAK TERSEDIA"]
        failed = [x["Kriteria"] for x in self.checks if x["Status"] == "GAGAL"]
        self.reasons = failed + missing
        self.status = "NO_TRADE" if failed else ("WAIT" if missing else "VALID")
        if self.status == "VALID":
            self.plan = self.candidate
        return self
