"""Optional lightweight 24/7 scanner worker for CoinAgentX Simple Reversal (v7.0.10)."""
from __future__ import annotations

import argparse
import json
import os
import time

from simple_engine import (
    SIZING_MARGIN_PCT,
    SIZING_MARGIN_USD,
    SIZING_RISK_PCT,
    ScanSettings,
    SimpleTradingEngine,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="CoinAgentX Simple Reversal worker (paper only)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    parser.add_argument("--symbols", type=int, default=int(os.getenv("SCAN_SYMBOLS", "50")))
    parser.add_argument("--initial-equity", type=float, default=float(os.getenv("PAPER_INITIAL_EQUITY", "1000")))
    parser.add_argument("--sizing-mode", choices=[SIZING_MARGIN_PCT, SIZING_MARGIN_USD, SIZING_RISK_PCT],
                        default=os.getenv("PAPER_SIZING_MODE", SIZING_MARGIN_PCT))
    parser.add_argument("--margin-pct", type=float, default=float(os.getenv("PAPER_MARGIN_PCT", "5.0")))
    parser.add_argument("--margin-usd", type=float, default=float(os.getenv("PAPER_MARGIN_USD", "50.0")))
    parser.add_argument("--risk-pct", type=float, default=float(os.getenv("PAPER_RISK_PCT", "0.5")))
    parser.add_argument("--leverage", type=int, default=int(os.getenv("PAPER_LEVERAGE", "10")))
    parser.add_argument("--tp-multiple", type=float, default=float(os.getenv("PAPER_TP_MULTIPLE", "2.0")))
    parser.add_argument("--no-trailing", action="store_true")
    parser.add_argument("--no-guardrails", action="store_true")
    parser.add_argument("--no-sl-filter", action="store_true")
    parser.add_argument("--min-sl-pct", type=float, default=float(os.getenv("PAPER_MIN_SL_PCT", "0.25")))
    parser.add_argument("--max-sl-pct", type=float, default=float(os.getenv("PAPER_MAX_SL_PCT", "2.0")))
    parser.add_argument("--no-auto-leverage", action="store_true")
    parser.add_argument("--max-sl-roe-pct", type=float, default=float(os.getenv("PAPER_MAX_SL_ROE_PCT", "30.0")))
    parser.add_argument("--sl-cooldown-min", type=int, default=int(os.getenv("PAPER_SL_COOLDOWN_MIN", "60")))
    parser.add_argument("--max-consecutive-sl", type=int, default=int(os.getenv("PAPER_MAX_CONSECUTIVE_SL", "2")))
    parser.add_argument("--mute-hours", type=int, default=int(os.getenv("PAPER_MUTE_HOURS", "24")))
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.interval < 30:
        raise SystemExit("--interval minimal 30 detik")
    trailing_enabled = not args.no_trailing
    tp_multiple = max(0.0, float(args.tp_multiple))
    if not trailing_enabled and tp_multiple == 0.0:
        tp_multiple = 2.0
    engine = SimpleTradingEngine(secret_getter=os.environ.get)
    settings = ScanSettings(
        symbols=max(1, min(200, args.symbols)),
        interval_seconds=args.interval,
        initial_equity=max(50.0, args.initial_equity),
        sizing_mode=args.sizing_mode,
        margin_pct=min(100.0, max(0.5, args.margin_pct)),
        margin_usd=max(1.0, args.margin_usd),
        risk_pct=min(10.0, max(0.1, args.risk_pct)),
        leverage=max(1, min(50, args.leverage)),
        send_telegram=not args.no_telegram,
        trailing_stop_enabled=trailing_enabled,
        tp_multiple=tp_multiple,
        guardrails_enabled=not args.no_guardrails,
        sl_cooldown_minutes=max(0, args.sl_cooldown_min),
        max_consecutive_sl=max(2, args.max_consecutive_sl),
        mute_hours=max(0, args.mute_hours),
        sl_filter_enabled=not args.no_sl_filter,
        min_sl_pct=max(0.0, float(args.min_sl_pct)),
        max_sl_pct=max(0.0, float(args.max_sl_pct)),
        auto_leverage_enabled=not args.no_auto_leverage,
        max_sl_roe_pct=min(65.0, max(10.0, float(args.max_sl_roe_pct))),
    )
    print("CoinAgentX Simple Reversal worker dimulai · PAPER ONLY · BB+RSI + Step R-Multiple Trailing", flush=True)
    while True:
        started = time.monotonic()
        report = engine.scan(settings)
        compact = {k: report.get(k) for k in ("status", "provider", "instrument_kind", "scanned", "m5_fetched", "duration_seconds", "failed", "completed_ms", "error", "note")}
        compact["new_signals"] = [
            {"symbol": s["symbol"], "direction": s["direction"], "entry": s["entry"], "stop": s["stop"], "target": s["target"], "margin_usdt": s.get("margin_usdt"), "leverage": s.get("leverage")}
            for s in report.get("signals", [])
        ]
        print(json.dumps(compact, ensure_ascii=False), flush=True)
        if args.once:
            break
        sleep_for = max(1.0, args.interval - (time.monotonic() - started))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
