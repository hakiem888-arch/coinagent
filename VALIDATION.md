# Laporan Validasi & Regresi Teknis — CoinAgent X / Futures Desk v6.0.3 (dan kompatibel v6.0.2)

Tanggal laporan: 14 November 2026 WIB.
Status: **LULUS REMEDIASI AUDIT TAHAP 1 (`BUG-01`..`BUG-08`), TAHAP 2 (`BUG-09`..`BUG-14`), & TAHAP 3 (`BUG-15`..`BUG-21`)**.

---

## 1. Hasil Eksekusi Kompilasi & Pengujian Lengkap (v6.0.2)

- **Kompilasi Statis (`compileall`):** Seluruh **51 file Python** (`app.py`, `manual_app.py`, `worker.py`, `check_market_access.py`, paket `futures/`, dan `tests/`) berhasil dikompilasi tanpa error.
- **Total Pengujian Lulus:** **267 passed, 0 failed** di seluruh **26 modul pengujian** (termasuk `test_audit_v602_deployment.py` [BUG-15..BUG-21], `test_audit_v6_security_failover.py` [BUG-09..BUG-14], `test_audit_remediation.py` [BUG-01..BUG-08 & C01–C12], `test_dashboard.py`, dan `test_ui.py`).

---

## 2. Matriks Remediasi Audit Tahap Ketiga (BUG-15 s/d BUG-21)

| Kode Temuan | Prioritas | Permasalahan Sebelumnya | Tindakan Perbaikan pada v6.0.2 | Pengujian Verifikasi |
| :--- | :--- | :--- | :--- | :--- |
| **BUG-15** | KRITIS | Tanpa token admin/env flag, `auth_required` bernilai `False` dan sesi anonim mendapat role `ADMIN`. | Menetapkan `auth_required = True` secara default di `SecurityManager`. Sesi anonim selalu mendapat role `VIEWER` dan ditolak saat memicu scan. Menambahkan `verify_public_deployment_startup()`. | `test_bug15_default_auth_is_strict_and_anonymous_is_always_viewer`, `test_public_viewer_defaults_off_and_cannot_trigger_server_actions` |
| **BUG-16** | KRITIS | Otorisasi `TELEGRAM_ALERT` dan `AI_REVIEW` belum dipanggil di seluruh jalur otomatis, dan toggle dashboard aktif default untuk publik. | Memasang gerbang `SecurityManager.authorize_action()` di dalam `GroqRotation.review()`, seluruh metode `TelegramAlerts`, dan `MarketScanner.run()`. Menerapkan kuota AI global per jam & per sesi. Mengatur default toggle pengunjung publik ke `False`. | `test_bug16_ai_review_telegram_and_autoscan_enforce_authorization_and_global_quota`, `test_public_viewer_defaults_off_and_cannot_trigger_server_actions` |
| **BUG-17** | KRITIS | SQLite lokal tidak menjamin persistensi saat reboot/redeploy di Streamlit Community Cloud. | Menambahkan `futures/db_backend.py` dengan dukungan PostgreSQL/Supabase (`DATABASE_URL` / `SUPABASE_URL`), transaksi database atomik (`atomic_transaction`), serta ekspor & pemulihan backup JSON bebas duplikasi (`import_backup_json`). | `test_bug17_backup_and_restore_preserves_state_without_duplicating_positions_or_alerts` |
| **BUG-18** | TINGGI | Worker 24/7 tidak boleh bergantung pada sesi browser dashboard yang dapat tidur setelah 12 jam. | Memisahkan scheduler `worker.py` (dengan token `ROLE_WORKER` dan pencatatan `worker_heartbeat`) dari dashboard *read-only*, serta menerapkan *lease lock* database `scanner_locks` + `ProcessLock`. | `test_bug18_single_scanner_db_lease_lock_and_worker_heartbeat` |
| **BUG-19** | MENENGAH | Shim `st_compat` memakai `except Exception` yang dapat menutupi kerusakan instalasi Streamlit produksi. | Membatasi `install_if_missing()` hanya pada mode pengujian (`is_test_environment()`) dan menggunakan `importlib.util.find_spec("streamlit")` secara spesifik. | `test_bug19_st_compat_never_activates_outside_test_mode` |
| **BUG-20** | MENENGAH | `_bootstrap_project_files()` hanya memeriksa keberadaan file/marker tanpa memverifikasi integritas konten. | Menambahkan manifest hash `_EMBEDDED_MANIFEST_SHA256` dan `_EMBEDDED_BUNDLE_SHA256`, membandingkan hash SHA-256 setiap file aktual, dan memulihkan file rusak secara atomik melalui direktori *staging* + `os.replace()`. | `test_bug20_self_bootstrapping_verifies_sha256_manifest_and_repairs_tampered_files` |
| **BUG-21** | RENDAH | Dokumentasi dan beberapa pesan notifikasi masih mencantumkan versi `6.0.0` / `5.9.9`. | Menyatukan konstanta `APP_VERSION = "6.0.2"` di `futures/version.py` dan menggunakannya di seluruh kode, notifikasi Telegram, `README.md`, dan `VALIDATION.md`. | `test_bug21_unified_app_version_6_0_2` |

---

## 3. Ringkasan Kepatuhan Kriteria Release v6.0.2

1. **Sesi anonim tidak bisa memperoleh ADMIN pada deployment publik:** Terpenuhi (`BUG-15`).
2. **AI dan Telegram tidak dapat dipanggil tanpa otorisasi yang benar:** Terpenuhi (`BUG-16`).
3. **Data jurnal tetap tersedia setelah reboot dan redeploy (PostgreSQL/Supabase & Backup/Restore Idempotent):** Terpenuhi (`BUG-17`).
4. **Worker dapat berjalan independen dari sesi browser dengan satu scanner per database:** Terpenuhi (`BUG-18`).
5. **Shim pengujian tidak menutupi Streamlit produksi & seluruh CI otomatis terkonfigurasi:** Terpenuhi (`BUG-11`, `BUG-19`).
6. **Failover tidak mencampur posisi antar-instrumen:** Terpenuhi (`BUG-09`, `BUG-12`).
7. **Self-Bootstrapping memverifikasi integritas SHA-256 seluruh file secara atomik:** Terpenuhi (`BUG-20`).
8. **Hasil PAPER tetap realistis dan tidak mengklaim profitabilitas tanpa bukti backtest:** Terpenuhi (`BUG-01`..`BUG-08`, `BUG-14`).


## 4. Riwayat Audit Sebelumnya
- **v5.9.9 (13 November 2026):** Remediasi penuh `BUG-01` s/d `BUG-08` dan 12 skenario simulasi wajib Bagian C.


### Remediasi Audit Regresi v6.0.2 (BUG-22 s/d BUG-26)
- **BUG-22 (PostgreSQL False Positive & Konfigurasi Terpusat)**: `resolve_database_config` meneruskan `DATABASE_URL` ke seluruh instance `PaperLedger`, `SecurityManager`, `TelegramAlerts`, dan `worker.py`; `requirements.txt` menyertakan `psycopg[binary]>=3.1.18,<4.0.0`; `connect_database` menolak fallback diam-diam ke SQLite (`DatabaseConfigurationError`); dan `persistence_status` hanya melaporkan `POSTGRESQL` (`persistent=True`) bila koneksi dan probe baca/tulis berhasil.
- **BUG-23 (Sinkronisasi Aktif Supabase REST)**: `PaperLedger` otomatis memanggil `SupabaseStateSync.push_backup` setelah setiap transaksi penting dan otomatis memulihkan state saat SQLite lokal kosong pasca-restart, dengan `namespace` terpisah per instrumen, `schema_version=2`, `checksum_sha256`, penolakan backup kedaluwarsa (`stale guard`), dan retry jaringan.
- **BUG-24 (Health Check Nyata SQLite vs PostgreSQL)**: Menghapus translasi palsu `PRAGMA quick_check -> SELECT 'ok'` pada adapter PostgreSQL dan memisahkan `verify_sqlite_integrity`, `verify_postgres_health`, serta `verify_database_health`.
- **BUG-25 (Kuota AI Global & Per-Sesi Persisten Lintas Proses)**: Kuota AI disimpan secara atomik pada tabel bersama `security_quota_events` sehingga konsisten antara `worker.py`, `app.py`, dan bertahan melintasi restart proses.
- **BUG-26 (Fingerprint Cache Resource & Rotasi Token Admin)**: `compute_auth_config_fingerprint` memastikan `@st.cache_resource` diperbarui saat token dirotasi tanpa menyimpan token asli di log/memori, dan otomatis membatalkan seluruh sesi administrator sebelumnya (`TOKEN_ROTATION`).


### Remediasi Audit Regresi Terbaru v6.0.2 (BUG-27 s/d BUG-32, LOGIC-01, & OPS-01..03)
- **Total Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`OPS-03`).
- **BUG-27 (Dynamic Leverage pada Plan)**: `select_dynamic_leverage()` selalu mengembalikan `max_allowed_leverage` dan `selected_leverage` pada semua jalur return (tanpa `KeyError`), menolak sinyal dengan `confidence < 85`, dan menerapkan `effective_leverage = selected_lev` pada `plan['leverage']`.
- **BUG-28 (Canonical Checksum & Pemulihan Audit Log)**: `compute_backup_checksum()` menghitung `trades`, `events`, `meta`, `telegram_alerts`, `security_audit_log`, `security_quota_events`, `instrument_kind`, dan `namespace`; mewajibkan `checksum_sha256` pada seluruh backup schema v2+; menolak backup lintas instrumen; serta memulihkan `security_audit_log` secara idempoten saat restore.
- **BUG-29 (Perpindahan Scanner Lock Atomik saat Failover)**: `MarketScanner` menyimpan `lock_owner_ledger` dan `lock_owner_path`, mengakuisisi lock database tujuan secara atomik sebelum melepas lock sumber saat failover, dan membatalkan scan jika database tujuan sedang dikunci scanner lain.
- **BUG-30 (Klasifikasi Error Eksplisit untuk Fallback Spot)**: `classify_provider_error()` memisahkan `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `NETWORK_ERROR`, dan `INVALID_RESPONSE`; fallback ke `SPOT_REFERENCE` hanya terjadi pada `REGIONAL_RESTRICTION` (HTTP 403/451 regional), tidak pernah pada `http_status=None`, timeout jaringan, JSON/retCode invalid, atau rate limit.
- **BUG-31 (Otorisasi Tunggal Telegram)**: Menghapus otorisasi ganda pada `send_summary()`, `send_watchlist()`, dan `send_cancellation()` sehingga 1 pengiriman hanya menggunakan 1 kuota `TELEGRAM_ALERT` dan 1 catatan audit.
- **BUG-32 (Diagnostik Database Sesuai Backend Aktual)**: Dashboard menggunakan `ledger.integrity_check()` (`database_health`) sesuai backend aktif (`POSTGRESQL` vs `SQLITE`).
- **LOGIC-01 (Eksekusi Otomatis TP3 4R & Runner)**: `PaperLedger.advance()` mengeksekusi `TP1` (2R, 50%), `TP2` (3R, 25%), dan `TP3` (4R, 25% runner) secara otomatis pada quote order-book valid sembari mengawal runner dengan trailing stop 2 ATR setelah `TP2`.
- **OPS-01 (SQL Setup Supabase & PostgreSQL)**: Disediakan pada `supabase/migrations/001_coinagent_schema.sql` (mencakup tabel `public.coinagent_backups` dengan `UNIQUE (namespace)` dan kolom `payload JSONB` + RLS policy `service_role`). Jalankan di Supabase SQL Editor lalu verifikasi via panel diagnostik dashboard (`verified_rw: true`).
- **OPS-02 (Guard Keamanan Legacy `manual_app.py`)**: `manual_app.verify_legacy_entrypoint_security()` memblokir penggunaan tanpa autentikasi pada deployment publik (`COINAGENT_PUBLIC_DEPLOYMENT=true`); gunakan `app.py` sebagai entrypoint produksi.


### Remediasi Audit Supabase Integration & Security v6.0.2 (16 November 2026 — SUPABASE-01..03, SEC-03, BUG-35, OPS-03, QA-03, DOC-03)
- **Status Pengujian & CI Terverifikasi (`DOC-03`)**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** pada tanggal **16 November 2026** (GitHub Actions CI: `https://github.com/hakiem888-arch/secretagent/actions/workflows/ci.yml`).
- **SUPABASE-01 (Explicit GRANT/REVOKE & Live RW Probe)**: `supabase/migrations/001_coinagent_schema.sql` memberikan `GRANT USAGE ON SCHEMA public TO service_role`, `REVOKE ALL ... FROM PUBLIC, anon, authenticated`, serta `GRANT SELECT, INSERT, UPDATE, DELETE` khusus `service_role`; `SupabaseStateSync.verify_rw_probe()` memverifikasi upsert, select, update, dan pull nyata.
- **SUPABASE-02 (Timestamp Checksum & HMAC Envelope Auth)**: `backup_at_ms`, `state_updated_ms`, dan `state_seq` tercakup di dalam `compute_backup_checksum()` dan `compute_backup_hmac()`; ketidaksesuaian timestamp antara envelope dan payload langsung ditolak.
- **SUPABASE-03 (Single-Writer REST Guard & Non-ACID Labeling)**: `SupabaseStateSync` menolak konflik multi-writer (`SUPABASE-03 MULTI-WRITER CONFLICT`) serta snapshot kedaluwarsa (`STALE SNAPSHOT REJECTED`), dan tidak pernah mengklaim REST Sync sebagai database ACID bersama (`acid_transactional: false`, `shared_multi_writer_safe: false`).
- **SEC-03 (Proteksi Tabel Internal, `token_hash`, & Audit Failure Monitoring)**: Seluruh 12 tabel internal menerapkan `ENABLE/FORCE ROW LEVEL SECURITY` dan `REVOKE ALL FROM PUBLIC, anon, authenticated`; `security_sessions` hanya menyimpan digest SHA-256 `token_hash` (bukan bearer token mentah); kegagalan penulisan `security_audit_log` dipantau aktif melalui `audit_health_status()`.
- **BUG-35 (Klasifikasi Spesifik HTTP 403 Bybit)**: Memisahkan `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `AUTH_ERROR`, dan `UNKNOWN_403` berdasarkan isi respons dan `retCode`; `UNKNOWN_403` dan `AUTH_ERROR` tidak memicu fallback ke `SPOT_REFERENCE`.
- **OPS-03 & QA-03 (Monitoring Heartbeat Worker 24/7 & 10-Stage Integration Test)**: `PaperLedger.worker_health_status()` mendeteksi `WORKER_ALIVE`, `WORKER_DEAD_OR_DISCONNECTED`, dan `DB_CONNECTION_LOST`, diverifikasi melalui 10 skenario integrasi penuh pada `tests/test_audit_v602_supabase_security_qa03.py`.


### Remediasi Audit Arsitektur v6.0.2 (16 November 2026 — BUG-36 s/d BUG-44)
- **Status Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`tests/test_audit_v602_architecture_bug36_44.py`).
- **BUG-36 (Scanner Independen dari Sesi Admin & Status `WORKER_OFFLINE`)**: Eksekusi scanner pada Worker (`ROLE_WORKER`) berjalan independen dari masa berlaku token `ADMIN` (3.600 detik); menampilkan `WORKER_OFFLINE` secara eksplisit saat worker tidak aktif dan mencegah dua scheduler berjalan bersamaan.
- **BUG-37 (Pemisahan Refresh UI 60s dari Interval Scanner)**: Refresh tampilan pasif diatur menjadi `60s` (bukan `15s`) tanpa memicu request API market baru, dilengkapi informasi `last_scan_at` dan `next_scan_at`.
- **BUG-38 (Tabel Global `user_preferences`, Default `OFF`, & Stabilitas Toggle)**: Konfigurasi sidebar disimpan secara transaksional di tabel global `user_preferences` (terpisah dari ledger trading `paper_meta`), default fitur otomatis berisiko ditetapkan `OFF`, indikator `SAVED` / `SAVE_FAILED` ditampilkan secara eksplisit, dan pergantian role (`ADMIN` <-> `VIEWER`) maupun failover provider tidak pernah mengubah nilai toggle.
- **BUG-39 (Diagnostik Filter 7 Kategori Penolakan)**: `shortlist()` dan dashboard menampilkan rincian `rejected_low_volume`, `rejected_wide_spread`, `rejected_low_trades`, `rejected_stale_quote`, `rejected_missing_candle`, `rejected_invalid_symbol`, dan `rejected_intraday_range` beserta metrik ringkasan.
- **BUG-40 (Sinkronisasi Interval Global Admin, Viewer, & Worker)**: Satu sumber konfigurasi interval di `user_preferences` dibaca secara identik oleh Admin, Viewer (tanpa paksaan `300s`), dan `worker.py` secara dinamis setiap siklus tanpa restart.
- **BUG-41 (Tabel Global `scanner_runs` Lintas Provider)**: Setiap hasil scan disimpan ke tabel global `scanner_runs` sehingga dashboard selalu menampilkan laporan terbaru berdasarkan timestamp lintas `Binance`, `Bybit`, dan `Spot Reference`.
- **BUG-42 (Kelanjutan Technical Scan Saat News API Gagal)**: Kegagalan News API menetapkan `news_status = 'NEWS_UNAVAILABLE'` namun tetap menjalankan screening teknikal Tahap 1, Tahap 2 (M15), dan Tahap 3.
- **BUG-43 (Klasifikasi Spesifik Error M15)**: Error kandidat M15 diklasifikasikan secara spesifik ke dalam `HTTP_TIMEOUT`, `RATE_LIMIT`, `STALE_CANDLE`, `CANDLE_NOT_CLOSED`, `EMPTY_RESPONSE`, `INVALID_SYMBOL`, `INVALID_OHLC`, dan `CONNECTION_ERROR` dengan redaksi kredensial otomatis.
- **BUG-44 (Anggaran Waktu Per Tahap & `PARTIAL_SCAN` + Antrean Kandidat)**: Memisahkan anggaran waktu per tahap dan menandai siklus yang terpotong batas waktu sebagai `PARTIAL_SCAN` (bukan `COMPLETED`) beserta antrean `pending_candidate_queue` untuk siklus berikutnya.


### Remediasi Market Scanner & Aturan Buy/Sell v6.0.2 (17 November 2026 — BUG-45 s/d BUG-53 + Panel Diagnostik 19 Metrik)
- **Status Pengujian Terverifikasi**: **327 unit & integration test lulus (0 gagal) di 26 modul pengujian** (`tests/test_audit_v602_scanner_rules_bug45_53.py`).
- **BUG-45 (Shortlist 30, Balanced Long/Short, Rotating Queue 5 Kandidat/Siklus)**: Shortlist dinaikkan menjadi 30 kandidat dengan antrean rotasi 5 kandidat per siklus, keseimbangan LONG/SHORT, filter skor minimum M15 (`min_m15_deep_score=55`), serta pengiriman alert watchlist bagi kandidat memenuhi syarat di luar top 5.
- **BUG-46 (Pemisahan Tahap Alert `EARLY WATCH`, `SETUP FORMING`, `PAPER ENTRY`)**: Alert dibagi per tahap (`EARLY WATCH` ≥55, `SETUP FORMING` ≥65 + ≥3/5 konfirmasi M5, `PAPER ENTRY`) dengan deduplikasi `symbol + timeframe + candle_close + direction + stage` yang mengizinkan promosi tahap tanpa spam duplikat.
- **BUG-47 & BUG-48 (Hard Risk Gates vs Konfluensi Berbobot 3/5 & Profil Strategi)**: Seluruh Hard Risk Gates dipertahankan ketat sementara indikator lunak memakai sistem konfluensi berbobot (minimal 3 dari 5 konfirmasi inti M5) pada profil `CONSERVATIVE` (85, risiko ≤0.50%), `BALANCED` (78, risiko ≤0.35%), dan `ACTIVE` (72, risiko ≤0.25%) yang dilengkapi validasi *walk-forward*.
- **BUG-49 (Audit Komponen Confidence Score & Kalibrasi PAPER)**: Seluruh 10 komponen `compute_confidence` memberikan tepat 0 poin bila kondisi tidak terjadi, tidak pernah menyebut skor sebagai *win probability*, dan dikalibrasi terhadap metrik historis PAPER (`win_rate_pct`, `expectancy_r`, `profit_factor`, `max_drawdown_pct`).
- **BUG-50 (Pemisahan `RULE_BASED_VALID`, `AI_CONFIRMED`, & `AI_UNAVAILABLE`)**: AI berperan sebagai *contextual reviewer* tanpa dapat menciptakan sinyal ketika rule engine gagal, dan memisahkan status `AI_UNAVAILABLE` secara eksplisit saat Groq mengalami gangguan/cooldown.
- **BUG-51 (Kebutuhan Candle Berdasarkan Indikator + `HISTORY_INSUFFICIENT`)**: Kebutuhan histori disesuaikan dengan indikator aktual (`200` candle bias EMA200, `120` candle eksekusi M5) dengan tetap menjaga pemeriksaan kontinuitas candle dan status `HISTORY_INSUFFICIENT`.
- **BUG-52 (`ENTRY_BLOCKED_POSITION_LIMIT` Tanpa Mematikan Discovery)**: Saat 2 posisi/pending aktif, scanner tetap menyaring market, memperbarui watchlist, dan menampilkan kandidat terbaik dengan status `ENTRY_BLOCKED_POSITION_LIMIT`.
- **BUG-53 & Panel Diagnostik Wajib (19 Metrik Funnel)**: Evaluasi M15, M5, dan AI di-cache berdasarkan `symbol + timeframe + candle_close_ms`, serta setiap siklus scan menghasilkan 19 metrik funnel lengkap pada Panel Diagnostik Wajib.


### Implementasi CoinAgentX — Hybrid Adaptive Strategy Specification v1.0 (17 November 2026 — Deliverables 1–10 & Acceptance Tests 1–57)
- **Status Pengujian Terverifikasi**: **384 unit & integration test lulus (0 gagal) di 27 modul pengujian** (`tests/test_hybrid_adaptive_spec_v1_acceptance_1_57.py` mencakup seluruh **57 Acceptance Tests** `AT-01` s/d `AT-57`).
- **Deliverable 1 & 2 (`futures/indicators.py`, `futures/hybrid_adaptive.py`)**: M15 Regime Detector (`detect_m15_regime`) dengan prioritas `HIGH_VOLATILITY` -> `BREAKOUT_UP/DOWN` -> `STRONG_TREND_UP/DOWN` -> `TREND_UP/DOWN` -> `RANGE` -> `TRANSITION`, serta Deterministic Strategy Router (`route_strategy_by_regime`) + `ArmingStateStore` (`CONFLICT_WAIT` saat sinyal bertentangan).
- **Deliverable 3 (`evaluate_strategy_a`..`evaluate_strategy_e`)**: Modul terisolasi untuk `Strategy A` (*Trend Pullback*), `Strategy B` (*Breakout Retest* dalam 6 candle M5), `Strategy C` (*Momentum Scalping*), `Strategy D` (*Mean Reversion* khusus `RANGE`), dan `Strategy E` (*Liquidity Sweep Reversal* dengan prioritas di atas `D` pada `RANGE` dan satu-satunya strategi aktif pada `TRANSITION`).
- **Deliverable 4 (`compute_setup_quality_score`, `classify_setup_stage`)**: Sistem skor kualitas 100 poin (8 komponen) yang memetakan `55–64` ke `EARLY_WATCH`, `65–77` ke `SETUP_FORMING`, dan `>= 78` ke `ENTRY_CANDIDATE` / `PAPER_ENTRY` tanpa pernah menaikkan leverage atau risiko per transaksi.
- **Deliverable 5 (`evaluate_hybrid_adaptive`, `build_hybrid_entry_order`, `compute_hybrid_tp_targets`, `evaluate_hybrid_position_lifecycle`)**: Pemblokiran mutlak `SPOT_REFERENCE` untuk entri PAPER Futures, validasi `>= 250` candle tertutup & kontinu, Hard Gates likuiditas/spread/depth (`±10 bps >= 5x notional`), biaya round-trip `<= 20%` dari 1R, SL struktural `0,75–2,50 ATR M5`, Net R:R `>= 1,50R`, risiko `<= 0,25%` per posisi (`<= 0,50%` portofolio), *cooldown* `60 menit` setelah `3` rugi beruntun, order `STOP-LIMIT` (`MISSED_NO_CHASE`, kedaluwarsa `2` bar M5), serta exit parsial `40% (+1R) / 35% (+2R) / 25% (+3R)` + *net break-even* + *trailing* `1,25 ATR M5` pasca `+1,5R` + *time exit* `6` & `12` bar + `CONSERVATIVE_INTRABAR_APPROXIMATION`.
- **Deliverables 6–10 (`backtest_hybrid_adaptive.py`, `config/backtest_manifest.json`, `WALK_FORWARD_REPORT.md`, `CHANGELOG.md`, `tests/test_hybrid_adaptive_spec_v1_acceptance_1_57.py`)**: Mode *Advisor-Only* Groq (`AI_UNAVAILABLE` tidak memblokir `PAPER_ENTRY` valid), deduplikasi alert per tahap, antrean rotasi `50/30/10/5` + 17 metrik funnel, serta validasi *walk-forward* 6 lipatan (`60/20/20`) dengan uji stres biaya `1,5x` dan sensitivitas parameter `±10%`.

### Rilis CoinAgentX v6.0.4 — Hybrid Adaptive v1.1 (18 November 2026 — Futures Primary / 50-Candidate Pipeline)
- **Status Pengujian Terverifikasi**: **415 unit, integration, & acceptance test lulus (0 gagal) di 29 modul pengujian** (termasuk 26 Acceptance Tests v1.1 pada `tests/test_hybrid_adaptive_v1_1_acceptance_v604.py` dan 57 Acceptance Tests v1.0 pada `tests/test_hybrid_adaptive_spec_v1_acceptance_1_57.py`).
- **Futures Primary & `FUTURES_DATA_UNAVAILABLE`**: Prioritas `Binance USDⓈ-M Futures` -> `Bybit Linear Perpetual` -> `Spot Reference (REFERENCE ONLY)`, pemisahan mutlak data Futures vs Spot, dan penahanan `PAPER_ENTRY` Futures saat `FUTURES_DATA_UNAVAILABLE`.
- **Pipeline 50 Kandidat / Target 20 Dianalisis / Maksimal 5 Groq**: Rekonsiliasi penuh 10 metrik pipeline (`universe_total`, `stage1_passed`, `stage1_rejected`, `stage2_shortlist`, `queued_count`, `analyzed_count`, `deferred_count`, `expired_count`, `rejected_count`, `paper_entry_count`) dan antrean rotasi persisten (`scanner_candidate_queue`).
- **Worker 24/7 Heartbeat 30s/90s & Telemetri Adapter**: Heartbeat setiap 30 detik (stale > 90 detik, status `ONLINE`/`STALE`/`OFFLINE`/`NO_WORKER`) dan telemetri HTTP langsung pada adapter market, Groq, dan Telegram.

---
## CoinAgentX v6.0.5 — Predictive Watchlist v1.0 Validation Summary
- **Suite Tested**: 30 test modules (verified locally prior to final audit (superseded by 33-module suite below)).
- **New Acceptance Suite**: `tests/test_predictive_watchlist_v1_acceptance_v605.py` (14 comprehensive acceptance tests covering all 6 mandatory categories in Section J.7: Market Data, Predictive Engine, Lifecycle, Alert, Integration, Performance/Replay/UI).
- **PAPER ONLY Verification**: Static & runtime checks confirm zero live order-placement endpoints (`/fapi/v1/order`, `/v5/order/create`, `/api/v3/order`, `/sapi/v1/capital/withdraw`).
- **Offline Sandbox Blockers (Honest Disclosure)**:
  1. Real-market historical Binance/Bybit M1/M5/M15 OHLCV download for live out-of-sample calibration is **BLOCKED** in the offline sandbox environment (no outbound internet access). The zero-lookahead replay & walk-forward calibration engine (`futures/predictive_replay.py`) is implemented and unit-tested, ready to run against real Futures CSV/API data in a network-enabled environment.
  2. Live 24/7 production worker runtime heartbeat proof and live PostgreSQL/Supabase connection verification are **BLOCKED** in the offline sandbox (no internet / `psycopg` driver not pre-installed in VM). Fail-closed PostgreSQL enforcement (`COINAGENT_REQUIRE_POSTGRES=1`) and SQLite WAL persistence + single-worker `ProcessLock` lease are verified locally.

---
## CoinAgentX v6.0.5 Final Audit Validation (`P0-01`..`P1-06`)
- **Version**: `APP_VERSION = 6.0.5` | `SPEC_VERSION = Hybrid Adaptive v1.1 + Predictive Watchlist v1.0`
- **Verified Full Test Suite**: verified locally prior to remaining audit fixes (superseded by 33-module suite below) across **32 test modules** (including `tests/test_final_audit_v605.py`, `tests/test_p0_correction_v605.py`, `tests/test_predictive_watchlist_v1_acceptance_v605.py`).
- **Execution Safety**: 100% PAPER ONLY (zero live order endpoints).

---
## CoinAgentX v6.0.5 — Remaining Audit Fixes Validation (`P0-01`..`P1-06`)
- **Version**: `APP_VERSION = 6.0.5` | `SPEC_VERSION = Hybrid Adaptive v1.1 + Predictive Watchlist v1.0`
- **Local Offline Verification**: **453 passed, 0 failed, 0 skipped** across **33 test modules** (including `tests/test_remaining_audit_fixes_v605.py`, `tests/test_final_audit_v605.py`, `tests/test_p0_correction_v605.py`, and `tests/test_predictive_watchlist_v1_acceptance_v605.py`).
- **GitHub Actions CI Status**: Pending push to `hakiem888-arch/secretagent` (offline sandbox VM has no GitHub/outbound internet access; CI run URL will be generated automatically once pushed to `main`).
- **Execution Safety**: 100% PAPER ONLY (zero live order endpoints).

---
## CoinAgentX v6.0.6 Validation
- **Verified Local Suite**: **467 passed, 0 failed, 0 skipped** across **36 test modules** (`tests/test_v606_runtime_recovery.py` included).
- **Mode**: 100% PAPER ONLY.

## v7.0.1 Bybit endpoint/403 checks
- `python -m pytest -q`: 13 passed.
- `python -m py_compile app.py simple_engine.py worker.py futures/*.py check_market_access.py`: passed.
- Added mocked HTTP checks for the Indonesia endpoint, official global fallback order, generic 403 classification, and explicit geo-restriction classification.
- No live Bybit API access test was performed from this build environment; Streamlit Cloud must run `check_market_access.py` to verify its own egress path.
