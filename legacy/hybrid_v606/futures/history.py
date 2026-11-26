import json
from dataclasses import asdict
from contextlib import closing
from .db_backend import connect_database


def connect(path, pg_url=None, get_secret=None):
    conn = connect_database(path, pg_url=pg_url, get_secret=get_secret)
    conn.execute("CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, status TEXT, payload TEXT)")
    conn.commit()
    return conn


def save(path, result, final_status, ai, pg_url=None, get_secret=None):
    meta = result.meta or {}
    strategy_id = (
        meta.get("routed_strategy")
        or (result.plan or {}).get("strategy")
        or ("F" if meta.get("strategy_engine") == "HYBRID_ADAPTIVE_V1" else "LEGACY")
    )
    regime = (
        meta.get("m15_regime")
        or (result.plan or {}).get("m15_regime")
        or (result.metrics or {}).get("m15_regime")
        or "UNKNOWN"
    )
    data = {
        "baseline": asdict(result),
        "final_status": final_status,
        "ai": ai,
        "strategy_id": strategy_id,
        "regime": regime,
        "setup_quality_score": (result.metrics or {}).get("setup_quality_score", (result.metrics or {}).get("confidence_score", 0)),
    }
    ident = f"{meta['symbol']}:{meta['mode']}:{meta['time_ms']}"
    with closing(connect(path, pg_url=pg_url, get_secret=get_secret)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)",
            (ident, meta["time_ms"], meta["symbol"], meta["mode"], final_status, json.dumps(data, allow_nan=False)),
        )
        conn.execute("DELETE FROM analyses WHERE id NOT IN (SELECT id FROM analyses ORDER BY at_ms DESC LIMIT 2000)")
    return data


def load(path, limit=100, pg_url=None, get_secret=None):
    with closing(connect(path, pg_url=pg_url, get_secret=get_secret)) as conn, conn:
        return [json.loads(x[0]) for x in conn.execute("SELECT payload FROM analyses ORDER BY at_ms DESC LIMIT ?", (limit,))]
