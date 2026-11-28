# CoinAgentX Simple Reversal v7.0.9 (Fast + Level 1 & 2 SL Guardrails)

Scanner paper-trading Futures Perpetual berbasis **2 indikator murni** (**Bollinger Bands (20, 2) M15 + RSI (14) M5**), dilengkapi:
- **Adaptive Guardrails — Belajar dari Riwayat SL Supabase (Level 1 + Level 2):**
  - **Level 1 (Cooldown Pasca-SL):** Menjeda pair secara otomatis (default `60 menit`) setelah terkena `CLOSED_SL` / `CLOSED_LIQ` agar bot tidak langsung entry ulang melawan tren kuat.
  - **Level 2 (Auto-Mute Koin Rewel):** Otomatis mengistirahatkan pair (default `24 jam`) jika terkena **2x–3x SL beruntun** dalam 7 hari terakhir. Hitungan streak otomatis reset ke `0` begitu pair mencetak Win / Trailing TP / BE.
  - Pair yang sedang ditahan otomatis melewati pengunduhan candle (scan makin cepat & hemat API) dan tampil di panel **Memori SL Aktif** di dashboard.
- **Ukuran Posisi Perpetual Asli (`Margin × Leverage`):** Mode `% dari Modal`, `Nominal USD`, atau `Risiko Tetap`, lengkap dengan kolom **Margin (USDT)**, **Nilai Posisi**, **ROE (%)**, dan proteksi likuidasi terisolasi (`CLOSED_LIQ`).
- **Statistik Performa & Win Rate:** Win Rate (%), Win/Loss/BE, Profit Factor, Max Drawdown (%), Rata-rata Win/Loss, dan Kurva Ekuitas Realized.
- **Step-Based R-Multiple Trailing Stop (Opsi 1):** `+1.0R → BE (+0.05R)`, `+1.5R → +0.50R`, `+2.0R → +1.20R` dst. dengan opsi TP `2R / 3R / 4R / Open`.
- **Persistensi Supabase Permanen + Auto-Migrasi Kolom:** Mendukung `DATABASE_URL` (PostgreSQL Transaction Pooler) dan fallback `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
