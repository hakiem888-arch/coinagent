CoinAgentX Simple Reversal v7.0.5 — Persistensi Supabase Penuh + Step R-Multiple Trailing Stop + Bybit 403 Fix + Tabel Alert Telegram

Perubahan Utama (v7.0.5):
1. Persistensi Supabase Ganda (tidak hilang saat update/reboot Streamlit Cloud):
   - Prioritas 1 (Direkomendasikan): Supabase PostgreSQL langsung via `DATABASE_URL` / `SUPABASE_DB_URL` / `POSTGRES_URL` (gunakan Transaction Pooler port 6543 agar kompatibel IPv4 Streamlit Cloud).
   - Prioritas 2 (Otomatis jika tanpa DATABASE_URL): Supabase REST Snapshot Sync via `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (atau `SUPABASE_KEY`) ke tabel `coinagent_backups` (namespace `SIMPLE_REVERSAL_V7`). Data otomatis dipulihkan saat reboot dan otomatis dicadangkan setiap ada sinyal/trade/update trailing stop baru.
   - Dashboard menampilkan peringatan jika masih memakai SQLite lokal serta tombol "Cek koneksi & simpan Supabase" di expander Status scanner & diagnosa provider.
2. Semua fitur v7.0.4 tetap lengkap:
   - Step-Based R-Multiple Trailing Stop (Opsi 1): +1.0R -> BE (+0.05R), +1.5R -> +0.50R, +2.0R -> +1.20R (+0.50R tiap step berikutnya), plus opsi TP 2R/3R/4R/Open.
   - Rotasi endpoint resmi Bybit + proxy opsional + failover Tier 3 Bitget USDT Perpetual.
   - Tabel Hasil Analisis — Lolos Alert Telegram + filter & Download CSV.
