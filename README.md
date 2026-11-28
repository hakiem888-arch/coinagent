# CoinAgentX Simple Reversal v7.0.10 (SL% Filter 0.25%–2% + Auto-Leverage Anti-Likuidasi + Memori SL)

Scanner paper-trading Futures Perpetual berbasis **2 indikator murni** (**Bollinger Bands (20, 2) M15 + RSI (14) M5**), dilengkapi:
- **Filter Rentang SL% Sehat + Auto-Leverage Anti-Likuidasi (v7.0.10):**
  - Default **Min SL = `0.25%`** (menolak SL terlalu mepet yang rawan tersapu wick M5) & **Max SL = `2.0%`** (menolak SL candle ekstrem).
  - **Auto-Leverage:** Jika SL lebar, leverage efektif otomatis diturunkan agar kerugian SL tidak melewati batas ROE margin (default `≤ 30%`, maks keras `65%` — posisi mustahil terkena likuidasi terisolasi sebelum SL).
  - Format presisi harga pintar untuk koin murah seperti `PEPEUSDT` (hingga 8 desimal).
- **Adaptive Guardrails — Memori SL Supabase (Level 1 + Level 2):**
  - Level 1: Cooldown pasca-1x SL (default `60 menit`).
  - Level 2: Auto-Mute pasca-2x/3x SL beruntun dalam 7 hari (default `24 jam`); otomatis reset ke 0 setelah 1x Win/BE.
- **Ukuran Posisi Perpetual Asli (`Margin × Leverage`):** Mode `% dari Modal`, `Nominal USD`, atau `Risiko Tetap`, lengkap dengan kolom **Margin (USDT)**, **Nilai Posisi**, dan **ROE (%)**.
- **Statistik Performa & Win Rate:** Win Rate (%), Win/Loss/BE, Profit Factor, Max Drawdown (%), Rata-rata Win/Loss, dan Kurva Ekuitas Realized.
- **Step-Based R-Multiple Trailing Stop (Opsi 1):** `+1.0R → BE (+0.05R)`, `+1.5R → +0.50R`, `+2.0R → +1.20R` dst. dengan opsi TP `2R / 3R / 4R / Open`.
- **Persistensi Supabase Permanen + Auto-Migrasi Kolom:** Mendukung `DATABASE_URL` (PostgreSQL Transaction Pooler) dan fallback `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
