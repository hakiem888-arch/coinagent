"""Deterministic synthetic fixtures. Never fetched or presented as market data."""
import numpy as np
import pandas as pd
from futures.models import Snapshot, Contract, Account, INTERVAL_MS

NOW = int(pd.Timestamp("2026-10-24 12:00:10", tz="UTC").timestamp() * 1000)


def candles(close, interval, width=.1, now=NOW):
    close = np.asarray(close)
    step = INTERVAL_MS[interval]
    end = (now // step) * step
    times = np.arange(end - len(close) * step, end, step)
    opened = np.r_[close[0] - .01, close[:-1]]
    return pd.DataFrame({"open_time": times, "close_time": times + step - 1,
                         "open": opened, "high": np.maximum(opened, close) + width,
                         "low": np.minimum(opened, close) - width, "close": close,
                         "volume": np.full(len(close), 100.)})


def setup(seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + np.arange(700) * .02 + rng.normal(0, .15, 700)
    execution = candles(close, "15m")
    execution.loc[execution.index[-1], "volume"] = 150
    bias = candles(np.linspace(70, 112, 700), "4h", width=.3)
    last = float(close[-1])
    spec = Contract(.01, .001, .001, 10000, .01, .001, .001, 10000, .01, 100000, .5, 2)
    snap = Snapshot("BTCUSDT", "INTRADAY", NOW, bias, execution, spec, last, last, last-.001, last+.001,
                    {k: NOW-10 for k in ("last", "mark", "book", "depth")},
                    [[last-.001, 1000]], [[last+.001, 1000]],
                    previous_day_high=110, previous_day_low=100, funding_rate=.00001,
                    next_funding_ms=NOW + 3600000, funding_interval_hours=8,
                    open_bar_high=last+.01, open_bar_low=last-.01, open_mark_high=last+.01,
                    open_mark_low=last-.01, source="SYNTHETIC TEST ONLY")
    account = Account(account_day_utc="2026-10-24", account_verified=True, taker_fee=.0004,
                      exit_slippage_bps=.1, funding_reserve_rate=.00001, costs_verified=True,
                      news_verified_at_ms=NOW-1000, news_source="synthetic calendar fixture",
                      news_clear_from_ms=NOW-3600000, news_clear_until_ms=NOW+86400000,
                      order_semantics_verified=True)
    return snap, account


def verified(snap, account):
    from futures.strategy import evaluate
    result = evaluate(snap, account)
    if not result.candidate:
        raise AssertionError(result.reasons)
    p = result.candidate
    account.liquidation_price = p["sl"] - p["atr"] * 3 if p["direction"] == "LONG" else p["sl"] + p["atr"] * 3
    account.liquidation_source = "Synthetic exchange calculator fixture"
    account.liquidation_plan_id = p["id"]
    account.liquidation_checked_ms = NOW - 100
    return evaluate(snap, account)
