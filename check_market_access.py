"""Probe supported Futures-only market providers (Binance USD-M, Bybit Linear, Bitget USDT-Futures)."""
import json
import time

from futures.market import FuturesMarket, MarketError
from futures.bybit_market import BybitFuturesMarket
from futures.provider_router import BitgetFuturesMarket


def probe(label, market):
    started = time.monotonic()
    try:
        time_path = getattr(market, "time_path", "/fapi/v1/time")
        time_value = market.get(time_path)
        server_ms = int(time_value.get("serverTime") or 0)
        if server_ms <= 0:
            raise MarketError("Respons waktu server tidak valid")
        universe = market.universe()
        symbols = (universe.get("info") or {}).get("symbols") or []
        tickers = universe.get("tickers") or []
        if not symbols or not tickers:
            raise MarketError("Universe Futures kosong")
        return {"provider": label, "ok": True, "symbols": len(symbols), "tickers": len(tickers),
                "server_time_ms": server_ms, "duration_seconds": round(time.monotonic() - started, 2)}
    except Exception as exc:
        return {"provider": label, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:220]}",
                "http_status": getattr(exc, "http_status", None),
                "duration_seconds": round(time.monotonic() - started, 2)}


def diagnose():
    primary = probe("Binance USD-M Futures", FuturesMarket())
    if primary["ok"]:
        return {"ok": True, "active_provider": primary["provider"], "providers": [primary]}
    fallback = probe("Bybit USDT Linear Perpetual", BybitFuturesMarket(allow_host_fallback=True))
    if fallback["ok"]:
        return {"ok": True, "active_provider": fallback["provider"], "providers": [primary, fallback]}
    tertiary = probe("Bitget USDT Perpetual", BitgetFuturesMarket())
    if tertiary["ok"]:
        return {"ok": True, "active_provider": tertiary["provider"], "providers": [primary, fallback, tertiary]}
    return {"ok": False, "active_provider": None, "providers": [primary, fallback, tertiary],
            "next_step": "Seluruh endpoint Futures (Binance/Bybit/Bitget) tidak dapat diakses dari host ini. Pertimbangkan COINAGENT_BYBIT_PROXY / COINAGENT_MARKET_PROXY."}


def main():
    report = diagnose()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
