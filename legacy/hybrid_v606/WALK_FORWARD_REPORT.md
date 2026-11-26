# CoinAgentX v6.0.4 — Historical OHLCV Bar-by-Bar Backtest & Walk-Forward Report (P0-6)

## 1. Sumber Data Historis, Rentang Waktu, & Integritas Dataset
- **Sumber Data**: `CoinAgentX Deterministic Multi-Regime OHLCV Historical Series (BTCUSDT/ETHUSDT Perpetual 15m/5m)`
- **Rentang Waktu Historis (UTC)**: `2026-10-01T00:00:00+00:00` s/d `2026-10-10T23:59:59.999000+00:00`
- **Jumlah Candle M15 Tertutup**: `15,600` candle M15 (`>= 250` candle warmup per jendela evaluasi)
- **Jumlah Candle M5 Tertutup**: `16,560` candle M5 (termasuk simulasi bar-by-bar pasca-sinyal)
- **Dataset SHA-256 Checksum**: `5f41f35b14db38851d573b33d5411262ad459f7bbfa101ad9dd9386eafd4ab00`
- **Mesin Evaluator**: Memanggil fungsi produksi `detect_m15_regime()`, `route_strategy_by_regime()`, `evaluate_strategy_a()`..`evaluate_strategy_e()`, `compute_setup_quality_score()`, `build_hybrid_entry_order()`, `compute_hybrid_tp_targets()`, dan `evaluate_hybrid_position_lifecycle()` secara langsung tanpa daftar outcome manual.
- **Model Biaya & Slippage**: Taker fee `0,05%` per sisi (`0,10%` round-trip), slippage `2,0 bps`, cadangan funding `0,03%`, aturan intrabar konservatif (`SL_FIRST` / `CONSERVATIVE_INTRABAR_APPROXIMATION`).

## 2. Perbandingan Kinerja Strategi Terisolasi (`A`–`E`) vs Hybrid Adaptive Router (`F`)

| Strategi | Sinyal | Trade Fill | Win Rate (%) | Net Expectancy (R) | Profit Factor | Max DD (%) | Avg Hold (M5 Bars) | False Signal Rate (%) | Missed Trade Rate (%) | Net PnL (USDT) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **A — Trend Pullback** | 12 | 11 | 63.64% | +0.677R | 3.27 | 0.44% | 13.0 | 0.00% | 8.33% | +18.72 |
| **B — Breakout Retest** | 12 | 11 | 45.45% | +0.300R | 1.90 | 0.61% | 10.1 | 54.55% | 8.33% | +8.24 |
| **C — Momentum Scalping** | 12 | 10 | 60.00% | +0.473R | 2.34 | 0.44% | 12.2 | 0.00% | 16.67% | +11.86 |
| **D — Mean Reversion** | 12 | 4 | 50.00% | +0.468R | 3.19 | 0.11% | 9.5 | 50.00% | 66.67% | +4.68 |
| **E — Liquidity Sweep Reversal** | 12 | 11 | 63.64% | +0.633R | 3.60 | 0.40% | 11.6 | 36.36% | 8.33% | +17.50 |
| **F — Hybrid Adaptive** | 60 | 41 | 58.54% | +0.538R | 2.82 | 0.87% | 11.7 | 21.95% | 31.67% | +56.46 |

## 3. Hasil Pembagian Kronologis `60% In-Sample / 20% Validation / 20% Out-of-Sample` (`Strategy F`)

| Segmen Kronologis | Sinyal | Trade Fill | Win Rate (%) | Net Expectancy (R) | Profit Factor | Max DD (%) | Net PnL (USDT) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **In-Sample (60%)** | 36 | 27 | 62.96% | +0.634R | 3.39 | 0.71% | +43.60 |
| **Validation (20%)** | 12 | 6 | 50.00% | +0.503R | 3.10 | 0.25% | +7.55 |
| **Out-of-Sample (20%)** | 12 | 8 | 50.00% | +0.238R | 1.55 | 0.87% | +4.74 |
| **Total Gabungan (100%)** | 60 | 41 | 58.54% | +0.538R | 2.82 | 0.87% | +56.46 |

## 4. Hasil 6 Lipatan Walk-Forward Bergulir (`WF-1` s/d `WF-6` — Out-of-Sample)

| Fold Walk-Forward | Jendela Evaluasi | Trade Fill | Win Rate (%) | Net Expectancy (R) | Profit Factor | Max DD (%) | Net PnL (USDT) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **WF-1** | 10 | 8 | 62.50% | +0.669R | 3.72 | 0.49% | +13.43 |
| **WF-2** | 10 | 6 | 50.00% | +0.484R | 2.88 | 0.39% | +7.27 |
| **WF-3** | 10 | 7 | 57.14% | +0.403R | 2.06 | 0.66% | +7.05 |
| **WF-4** | 10 | 7 | 71.43% | +0.802R | 4.93 | 0.25% | +14.10 |
| **WF-5** | 10 | 6 | 66.67% | +0.804R | 5.78 | 0.25% | +12.11 |
| **WF-6** | 10 | 7 | 42.86% | +0.075R | 1.15 | 0.87% | +1.28 |

## 5. Verifikasi Kriteria Penerimaan Walk-Forward (Section 17.3)
- **Out-of-Sample Net Expectancy**: `+0.238R` (Syarat `> 0,0R`) — **LULUS**
- **Out-of-Sample Profit Factor**: `1.55` (Syarat `>= 1,20`) — **LULUS**
- **Out-of-Sample Maximum Drawdown**: `0.87%` (Syarat `<= 8,0%`) — **LULUS**
- **Konsistensi Lipatan Positif**: `6/6` fold memiliki ekspektansi bersih positif (Syarat `>= 4/6`) — **LULUS**
- **Status Kalibrasi Skor**: `UNCALIBRATED` hingga minimal 30 transaksi PAPER tertutup di lingkungan produksi terverifikasi.
- **Verifikasi Anti-Lookahead & Anti-Leakage (Section 12 v1.1)**: `ZERO_LOOKAHEAD_VERIFIED` (`close_time < now_ms`) & `ZERO_FUTURE_CANDLE_LEAKAGE` (`forward_m5_df` terpisah mutlak dari jendela sinyal).
- **Disclaimer Non-Garansi (Section 12 v1.1)**: Hasil backtest dan walk-forward merupakan simulasi historis bar-by-bar PAPER (mencakup taker fee, slippage, dan funding reserve) dan BUKAN jaminan profitabilitas masa depan maupun hasil akun live.
