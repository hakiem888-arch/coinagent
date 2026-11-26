# Changelog

## 7.0.0-simple

- Replaced the active Hybrid Adaptive strategy path with one deterministic reversal engine.
- Entry rules now use only M15 Bollinger Bands (20, 2), M5 RSI (14), and M5 candle reversal confirmation.
- Simplified the Streamlit dashboard to signals, paper positions, history, and a small settings panel.
- Replaced the old worker arguments and deploy service with the lightweight paper-only scanner.
- Kept the previous v6.0.6 source under `legacy/hybrid_v606/` as reference; it is not loaded by the active app.
