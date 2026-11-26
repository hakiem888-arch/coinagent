CoinAgentX Simple Reversal v7.0.2 — Bybit HTTP 403 Fix + Tier-3 Bitget Failover + Tabel Alert Telegram

Perubahan Utama:
1. futures/bybit_market.py:
   - Mendukung konfigurasi proxy opsional (COINAGENT_BYBIT_PROXY / COINAGENT_MARKET_PROXY) dan override COINAGENT_BYBIT_BASE secara dinamis.
   - Rotasi endpoint resmi (api.bybit.id -> api.bytick.com -> api.bybit.com) dengan deteksi blokir CloudFront/WAF.
   - Menambahkan helper server_time_ms() dan perbaikan pembacaan ticker di monitor_snapshot().
2. futures/provider_router.py:
   - Menambahkan Bitget USDT-M Perpetual (BITGET_USDT_PERPETUAL via https://api.bitget.com) sebagai Tier-3 Futures Perpetual Fallback saat Binance (HTTP 451) dan Bybit (HTTP 403/451) ditolak oleh IP server hosting.
   - Failover otomatis menangani HTTP 403 generik (UNKNOWN_403) maupun REGIONAL_RESTRICTION tanpa beralih ke Spot.
3. simple_engine.py:
   - Mengizinkan instrumen BITGET_USDT_PERPETUAL sebagai sumber Perpetual Futures valid.
   - Menambahkan SimpleStore.latest_telegram_signals() dengan kalkulasi SL%, TP%, R:R, Delta RSI, dan status kirim Telegram.
4. app.py:
   - Menambahkan Tabel Hasil Analisis — Lolos Alert Telegram lengkap dengan metrik ringkas, filter (status kirim, arah LONG/SHORT, pencarian pair), dan tombol Download CSV.
   - Menambahkan tombol "Cek koneksi provider (Binance / Bybit / Bitget)" di expander Status scanner & diagnosa provider.
   - Membaca proxy & base URL opsional dari Streamlit Secrets sebelum modul market diimpor.
5. check_market_access.py:
   - Memeriksa ketiga tier Futures Perpetual (Binance USD-M -> Bybit Linear -> Bitget USDT-Futures).
