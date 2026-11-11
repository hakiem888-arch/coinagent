from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

TIMEFRAMES = {"SCALPING": ("1h", "5m"), "INTRADAY": ("4h", "15m"), "SWING": ("1d", "4h")}
INTERVAL_MS = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
SPOT_SIMULATION_CHECKS = frozenset({"Deviasi Mark–Last", "Funding kontrak"})
DYNAMIC_LEVERAGE_OPTIONS = (2, 3, 5, 10, 15, 20)


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
