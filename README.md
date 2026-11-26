# CoinAgentX Simple Reversal v7.0

A deliberately small, paper-only crypto Futures scanner. The old Hybrid Adaptive strategy is no longer used by `app.py` or `worker.py`.

## Strategy — only two indicators

- **Bollinger Bands (20, 2)** on closed **M15** candles. A recent closed M15 candle must touch the lower band for a LONG setup or the upper band for a SHORT setup.
- **RSI (14)** on closed **M5** candles. LONG requires the prior RSI to be at or below 30 and the latest RSI to rise. SHORT requires the prior RSI to be at or above 70 and the latest RSI to fall.
- **M5 candle confirmation:** LONG requires the latest closed candle to be bullish and have a higher high and higher close than the previous closed candle. SHORT requires a bearish candle with a lower low and lower close.
- M5 confirmation must close after the M15 touch candle. Signals use closed candles only.

There are no regime classifiers, hybrid routes, EMA/MACD/Stochastic/ADX/VWAP/ATR filters, confidence scores, Groq decisions, news gates or AI vetoes in the new signal path.

## Paper trading

A confirmed signal is recorded in the paper journal. Entry uses the confirming M5 close, the stop uses the two-candle M5 extreme, and target is 2R. Position quantity is calculated from the configured paper balance and risk percentage, capped by the selected paper leverage. If one M5 candle reaches both stop and target, stop is assumed first. **No exchange order is submitted.**

## Run Streamlit

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app scans Futures perpetual markets only: Binance USD-M first, then Bybit Linear Futures if the primary provider is region-blocked. It does not silently substitute Spot data.

## Optional 24/7 worker

```bash
python worker.py --interval 60 --symbols 50 --initial-equity 1000 --risk-pct 0.5 --leverage 2
```

The Streamlit app and worker share a short database scan lease so they do not scan simultaneously.

## Secrets and persistence

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Streamlit Secrets or worker environment variables to enable Telegram alerts. Without them, signals and paper trades are still recorded.

- Set `DATABASE_URL` to a PostgreSQL/Supabase pooler connection for durable shared persistence between Streamlit and the worker.
- Without `DATABASE_URL`, the app uses `data/simple_coinagent.sqlite3`.
- Optional `COINAGENT_DB_PATH` changes the SQLite file path.

## Run checks

```bash
pytest -q
```

## Legacy code

The previous v6.0.6 Hybrid Adaptive app, worker, tests and strategy modules are kept under `legacy/hybrid_v606/` as reference only. The active `app.py`, `worker.py` and `simple_engine.py` do not import or execute that strategy.
