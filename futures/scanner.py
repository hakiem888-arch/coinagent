"""One bounded scan at a time; no worker loop and no live order API."""
import threading
import time
from dataclasses import dataclass
from .groq_rotation import combine
from .strategy import evaluate

SCAN_INTERVALS = (30, 45, 60, 90)


@dataclass
class ScanSchedule:
    enabled: bool = False
    context: str = ""
    next_due: float = 0.0
    running: bool = False

    def claim(self, now, enabled, interval, context, manual=False):
        if interval not in SCAN_INTERVALS:
            raise ValueError("Interval scan harus 30, 45, 60, atau 90 detik")
        if enabled and (not self.enabled or context != self.context):
            self.next_due = now
        self.enabled, self.context = enabled, context
        if self.running or not (manual or (enabled and now >= self.next_due - .25)):
            return False
        self.running = True
        # Reserve before any I/O. A UI rerun cannot start the same scan twice.
        self.next_due = now + interval
        return True

    def finish(self, now, interval):
        self.running = False
        # If a slow API exceeded the interval, skip missed ticks, never catch up.
        if now >= self.next_due:
            self.next_due = now + interval


class ScanBusy(RuntimeError):
    pass


class ScanEngine:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock

    def run(self, market, symbol, mode, account, pool, model):
        if not self.lock.acquire(blocking=False):
            raise ScanBusy("Scan lain sedang berjalan. Menunggu jadwal berikutnya.")
        try:
            snap = market.load(symbol, mode)
            acquired = self.clock()
            result = evaluate(snap, account)
            # Every acquired scan receives AI review, including WAIT/NO_TRADE.
            # combine() prevents AI from overriding a failed mandatory gate.
            ai = pool.review(result, model)
            if combine(result, ai) == "VALID":
                reviewed_id = result.plan["id"]
                snap = market.load(symbol, mode)
                acquired = self.clock()
                result = evaluate(snap, account)
                if not result.plan or result.plan["id"] != reviewed_id:
                    ai = {"decision": "WAIT", "reasons": [
                        "Setup atau data berubah setelah respons AI. Analisis ulang diperlukan."],
                        "cautions": [], "ok": False}
            return {"snapshot": snap, "result": result, "ai": ai,
                    "status": combine(result, ai), "monotonic": acquired}
        finally:
            self.lock.release()
