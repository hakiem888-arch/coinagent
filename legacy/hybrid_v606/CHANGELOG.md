## [6.0.6] — P0 Runtime Recovery & Futures Data (Hybrid Adaptive v1.1 + Predictive Watchlist v1.0 · PAPER ONLY)
- **P0-1 (Worker 24/7)**: Added `pid` and `worker_scans` verification to `PaperLedger.record_worker_heartbeat()` and graceful shutdown `WORKER_OFFLINE`/`MONITOR_OFFLINE` reporting in `worker.py`.
- **P0-2 (Predictive Monitor)**: Added `monitor_health == "HEALTHY"` when `MONITOR_ONLINE` with verified heartbeat & active feed; clear startup (`MONITOR_STARTING`) and shutdown (`MONITOR_OFFLINE`) lifecycle.
- **P0-3 (Market Stream & REST Fallback)**: Added `last_successful_update_ms`, preserved valid cached candles across cycle re-subscriptions, and ensured stale feed blocks early/confirmed entries.
- **P0-4 (Futures Provider)**: Added `validate_futures_contract_spec()` enforcing `TRADING`, `PERPETUAL`, `USDT`, `tick > 0`, `step > 0`, `min_qty > 0`, `min_notional > 0` while keeping Spot strictly isolated from Futures `PAPER_ENTRY`.
- **P1-1 (Budget & Telemetry)**: Aligned `stage_timings["ai_analysis_s"] == budget_telemetry["groq_request_time_s"]` and `budget_telemetry["total_elapsed_s"] == duration_seconds` on a single monotonic clock with `queue_wait_included_in_total=True`.
- **P1-2 (Telegram Delivery)**: Added HTTP-verified `last_delivery_record` (`outbox_id`, `http_status`, `delivery_timestamp_ms`, `retry_state`, `last_error`) in `TelegramAlerts.flush_predictive_outbox()` & `telegram_status()`.
- **P1-3 (Resource Monitoring)**: Added `cpu_user_s`, `cpu_system_s`, and `MultiCycleResourceProfiler` (`rss_delta_from_start_mb`) in `futures/memory_profiler.py`.

