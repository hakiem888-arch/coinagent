"""Diagnose public USDT perpetual market access from this host, without AI or alerts."""
import json, time
from futures.market import FuturesMarket, MarketError
from futures.bybit_market import BybitFuturesMarket


def _check(label, market):
    started=time.monotonic(); report={"provider":label,"ok":False,"checks":[]}
    try:
        if label.startswith("Binance"):
            checks=(("/fapi/v1/time", lambda x:isinstance(x,dict) and int(x.get("serverTime",0))>0),
                    ("/fapi/v1/exchangeInfo", lambda x:isinstance(x,dict) and bool(x.get("symbols"))),
                    ("/fapi/v1/ticker/24hr", lambda x:isinstance(x,list) and bool(x)),
                    ("/fapi/v1/ticker/bookTicker", lambda x:isinstance(x,list) and bool(x)))
            for path, valid in checks:
                t=time.monotonic(); value=market.get(path)
                try: good=valid(value)
                except (TypeError,ValueError): good=False
                if not good: raise MarketError("Struktur respons tidak sesuai",http_status=200,endpoint=path)
                report["checks"].append({"endpoint":path,"ok":True,"duration_seconds":round(time.monotonic()-t,2)})
        else:
            value=market.get(getattr(market, "time_path", "/v5/market/time"))
            if int(value.get("serverTime",0))<=0: raise MarketError("Waktu server Bybit tidak valid",http_status=200)
            universe=market.universe()
            if not universe.get("info",{}).get("symbols") or not universe.get("tickers"):
                raise MarketError("Universe Bybit kosong",http_status=200)
            report["checks"]=[{"endpoint":"/v5/market/time","ok":True},{"endpoint":"/v5/market/instruments-info + tickers","ok":True}]
        report["ok"]=True
    except (MarketError,ValueError,TypeError,KeyError) as exc:
        report["error"]=str(exc)
        report["checks"].append({"endpoint":getattr(exc,"endpoint",None),"ok":False,"http_status":getattr(exc,"http_status",None)})
        report["diagnostic"]=exc.diagnostic() if isinstance(exc,MarketError) else {}
    report["duration_seconds"]=round(time.monotonic()-started,2)
    return report


def diagnose(primary=None, fallback=None):
    # Supplying a market is useful for deterministic unit tests and checks only that adapter.
    if primary is not None:
        result=_check("Binance USD-M Futures",primary)
        return {"ok":result["ok"],"source":getattr(primary,"base","Binance USD-M Futures"),
                "checked_at_ms":int(time.time()*1000),"checks":result["checks"],
                "error":result.get("error"),"diagnostic":result.get("diagnostic"),
                "next_step":"Endpoint futures utama dapat diakses." if result["ok"] else "Periksa akses host ke Binance Futures."}
    primary=FuturesMarket(); fallback=fallback or BybitFuturesMarket()
    a=_check("Binance USD-M Futures",primary)
    if a["ok"]: return {"ok":True,"active_provider":a["provider"],"providers":[a],"next_step":"Binance Futures dapat diakses dari host ini."}
    b=_check("Bybit USDT Linear Perpetual",fallback)
    return {"ok":bool(b["ok"]),"active_provider":b["provider"] if b["ok"] else None,"providers":[a,b],
            "next_step":"Provider cadangan Bybit dapat diakses; aplikasi dapat menggunakan data Bybit dengan label sumber yang jelas." if b["ok"] else "Kedua provider tidak dapat diakses. Periksa jaringan dan pembatasan layanan pada hosting."}


def main():
    report=diagnose(); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if report["ok"] else 2

if __name__=="__main__": raise SystemExit(main())
