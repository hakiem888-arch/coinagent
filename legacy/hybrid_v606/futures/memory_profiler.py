"""Bounded LRU Cache & Real Process Memory Profiling Utilities (CoinAgentX v6.0.5 P1-08).

Provides:
- `BoundedLRUDict`: O(1) bounded OrderedDict replacing unbounded eval/AI caches to prevent
  unbounded RSS growth over multi-hour worker/dashboard runs.
- `read_process_rss_mb()`: Reads actual OS Resident Set Size from `/proc/self/status` (VmRSS)
  or `resource.getrusage` fallback.
- `profile_scanner_memory()`: Captures real RSS MB, cache entry counts, and optional
  `tracemalloc` snapshot without guessing or fabricating leak claims.
"""
from __future__ import annotations

import os
import resource
import sys
import tracemalloc
from collections import OrderedDict
from typing import Any, Optional


class BoundedLRUDict(OrderedDict):
    """Drop-in dict replacement with strict maximum capacity (LRU eviction)."""

    def __init__(self, max_entries: int = 256):
        super().__init__()
        self.max_entries = max(16, int(max_entries))

    def __getitem__(self, key):
        val = super().__getitem__(key)
        self.move_to_end(key)
        return val

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return super().__getitem__(key)
        return default

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_entries:
            self.popitem(last=False)


def read_process_rss_mb() -> float:
    """Return actual current process RSS in megabytes (MB)."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return round(float(parts[1]) / 1024.0, 2)
    except OSError:
        pass
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes
        divisor = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0
        return round(float(ru) / divisor, 2)
    except Exception:
        return 0.0


def profile_scanner_memory(scanner: Any = None, stream_manager: Any = None) -> dict[str, Any]:
    """Capture actual process RSS and cache occupancy across scanner, market, and stream caches."""
    rss_mb = read_process_rss_mb()
    tm_current_mb = 0.0
    tm_peak_mb = 0.0
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        tm_current_mb = round(cur / (1024.0 * 1024.0), 3)
        tm_peak_mb = round(peak / (1024.0 * 1024.0), 3)

    caches: dict[str, int] = {}
    if scanner is not None:
        caches["m15_eval_cache"] = len(getattr(scanner, "m15_eval_cache", {}) or {})
        caches["m5_eval_cache"] = len(getattr(scanner, "m5_eval_cache", {}) or {})
        caches["ai_review_cache"] = len(getattr(scanner, "ai_review_cache", {}) or {})
        caches["rotating_candidate_queue"] = len(getattr(scanner, "rotating_candidate_queue", []) or [])
        caches["pending_candidate_queue"] = len(getattr(scanner, "pending_candidate_queue", []) or [])
        mkt = getattr(scanner, "market", None)
        active_mkt = getattr(mkt, "active", mkt)
        caches["market_closed_cache"] = len(getattr(active_mkt, "cache", {}) or {})
        caches["market_live_cache"] = len(getattr(active_mkt, "_live_cache", {}) or {})
        sm = stream_manager or getattr(scanner, "intrabar_stream", None)
        if sm is not None:
            caches["ws_subscriptions"] = len(getattr(sm, "_subscriptions", {}) or {})
            caches["ws_live_candles"] = len(getattr(sm, "_live_candles", {}) or {})
            caches["ws_closed_buffer_keys"] = len(getattr(sm, "_closed_buffer", {}) or {})
            caches["ws_latest_snapshots"] = len(getattr(sm, "_latest_snapshots", {}) or {})

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "pid": os.getpid(),
        "rss_mb": rss_mb,
        "cpu_user_s": round(float(ru.ru_utime), 4),
        "cpu_system_s": round(float(ru.ru_stime), 4),
        "tracemalloc_active": tracemalloc.is_tracing(),
        "tracemalloc_current_mb": tm_current_mb,
        "tracemalloc_peak_mb": tm_peak_mb,
        "cache_entries": caches,
    }


class MultiCycleResourceProfiler:
    """P1-3: Track RSS, CPU, and cache entry growth across multiple scan cycles."""

    def __init__(self):
        self.samples: list[dict[str, Any]] = []

    def record(self, scanner: Any = None, stream_manager: Any = None, cycle_label: str = "") -> dict[str, Any]:
        snap = profile_scanner_memory(scanner, stream_manager)
        snap["cycle"] = cycle_label or f"cycle-{len(self.samples) + 1}"
        base_rss = self.samples[0]["rss_mb"] if self.samples else snap["rss_mb"]
        snap["rss_delta_from_start_mb"] = round(snap["rss_mb"] - base_rss, 2)
        self.samples.append(snap)
        return snap
