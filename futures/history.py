import json
import sqlite3
from pathlib import Path
from dataclasses import asdict
from contextlib import closing


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, status TEXT, payload TEXT)")
    return conn


def save(path, result, final_status, ai):
    data = {"baseline": asdict(result), "final_status": final_status, "ai": ai}
    meta = result.meta
    ident = f"{meta['symbol']}:{meta['mode']}:{meta['time_ms']}"
    with closing(connect(path)) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)", (ident, meta["time_ms"], meta["symbol"], meta["mode"], final_status,
                                                                             json.dumps(data, allow_nan=False)))
        conn.execute("DELETE FROM analyses WHERE id NOT IN (SELECT id FROM analyses ORDER BY at_ms DESC LIMIT 2000)")
    return data


def load(path, limit=100):
    with closing(connect(path)) as conn, conn:
        return [json.loads(x[0]) for x in conn.execute("SELECT payload FROM analyses ORDER BY at_ms DESC LIMIT ?", (limit,))]
