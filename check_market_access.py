"""Check public scanner endpoints from THIS host, without AI, a ledger or alerts.

Run: python check_market_access.py
Exit code 0: these four initial endpoints passed. Exit code 2: access/data failed.
This is an access check, not a full strategy or integration test.
"""
import json
import time
from futures.market import MarketError
from futures.spot_market import SPOT_BASE, SpotReferenceMarket


def diagnose(market=None):
    market = market or SpotReferenceMarket()
    report = {"source": SPOT_BASE, "checked_at_ms": int(time.time() * 1000),
              "ok": False, "checks": []}
    checks = (
        ("/api/v3/time", lambda value: isinstance(value, dict) and isinstance(value.get("serverTime"), int) and value["serverTime"] > 0),
        ("/api/v3/exchangeInfo", lambda value: isinstance(value, dict) and isinstance(value.get("symbols"), list) and bool(value["symbols"])),
        ("/api/v3/ticker/24hr", lambda value: isinstance(value, list) and bool(value)),
        ("/api/v3/ticker/bookTicker", lambda value: isinstance(value, list) and bool(value)),
    )
    for path, valid in checks:
        started = time.monotonic()
        try:
            payload = market.get(path)
            if not valid(payload):
                raise MarketError("Struktur respons data tidak sesuai.", http_status=200, endpoint=path)
            report["checks"].append({"endpoint": path, "ok": True,
                                     "duration_seconds": round(time.monotonic() - started, 2)})
        except MarketError as exc:
            report["checks"].append(dict(exc.diagnostic(), ok=False, message=str(exc),
                                         duration_seconds=round(time.monotonic() - started, 2)))
            report["next_step"] = (
                "HTTP 451: periksa kelayakan akses wilayah/IP host dengan Binance atau penyedia hosting. "
                "Menambah API key tidak mengatasi pembatasan ini."
                if exc.access_blocked else "Periksa jaringan, respons API, atau cooldown pada host ini.")
            return report
    report["ok"] = True
    report["next_step"] = "Endpoint awal dapat diakses. Jalankan aplikasi untuk menguji candle, kalender, dan AI."
    return report


def main():
    report = diagnose()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
