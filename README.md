# CoinAgentX Simple Reversal v7.0.7 (Lean, Fast & Perpetual Sizing)

Scanner paper-trading Futures Perpetual murni berbasis **2 indikator saja** (**Bollinger Bands (20, 2) M15 + RSI (14) M5**), dilengkapi:
- **Ukuran Posisi Perpetual Asli (`Margin × Leverage`):**
  - `% dari Modal (Margin × Leverage)`
  - `Nominal USD (Margin × Leverage)`
  - `Risiko Tetap (% Risk ke SL)`
  - Lengkap dengan tampilan **Margin (USDT)**, **Nilai Posisi (USDT)**, **ROE (%)**, dan proteksi likuidasi terisolasi (`CLOSED_LIQ`).
- **Statistik Performa & Win Rate:** Win Rate (%), Win/Loss/BE, Profit Factor, Max Drawdown (%), Rata-rata Win/Loss, dan Kurva Ekuitas Realized.
- **Step-Based R-Multiple Trailing Stop (Opsi 1):** `+1.0R → BE (+0.05R)`, `+1.5R → +0.50R`, `+2.0R → +1.20R` dst. dengan opsi TP `2R / 3R / 4R / Open`.
- **Scan Cepat & Ringan:** 1x query posisi aktif per scan, pre-filter BB M15 (skip M5 jika tidak menyentuh band), cache candle per bar, fetch paralel 6 worker, 1x koneksi DB per refresh UI, dan seluruh folder `legacy/` & modul tidak terpakai telah dihapus.
- **Persistensi Supabase Permanen:** Mendukung `DATABASE_URL` (PostgreSQL Transaction Pooler) dan fallback `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
- **Failover 3-Tier Perpetual Futures:** Binance USD-M → Bybit Linear (rotasi endpoint & proxy opsional) → Bitget USDT Perpetual.
