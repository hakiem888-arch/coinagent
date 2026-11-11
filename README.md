# CoinAgent X / Futures Desk v5.9.0 — Binance USDT-M Futures PAPER Scanner

Versi ini menjadikan **Binance USD-M USDT Perpetual** sebagai sumber data utama scanner. Aplikasi tetap PAPER-only: tidak mengirim order ke Binance dan tidak memerlukan Binance API key. Data market diambil dari endpoint publik Futures (`fapi.binance.com`).

## Perubahan utama v5.9.0

- Sumber utama dashboard dan worker beralih dari Spot Reference ke Binance USDT-M Perpetual.
- Screening Stage 2 memakai candle **M15 tertutup**, bukan candle 1H yang sebelumnya salah diberi label sebagai M15.
- Universe shortlist diperluas menjadi 20 simbol; maksimal 20 kandidat diperiksa pada tahap M15 dan maksimal 1–2 kandidat diteruskan ke analisis mendalam.
- Ranking memprioritaskan skor setup M15, dengan aktivitas dan likuiditas sebagai faktor tambahan.
- Liquidity sweep, divergence RSI, CHoCH awal, Stoch RSI cross, dan retest diberi skor hanya bila kondisi terkonfirmasi; indikator yang belum terkonfirmasi tidak lagi mendapat skor hampir penuh.
- Menghapus patch yang mengubah timestamp quote lama menjadi seolah-olah baru. Quote kedaluwarsa sekarang gagal pada pemeriksaan freshness.
- Menghapus fallback evaluator yang dapat membuat rencana VALID di luar evaluator strategi utama.
- AI auditor tidak lagi diperintah untuk menyetujui sinyal hanya karena baseline berstatus VALID; AI boleh memveto jika menemukan kontradiksi atau risiko.
- Fallback likuiditas menengah hanya memperluas watchlist. Fallback ini tidak dapat melewati kalender berita atau hard gate entry.
- Telegram summary dan watchlist aktif secara default pada konfigurasi worker; interval worker default 300 detik.
- Konfigurasi scanner dan bobot skor berada di `config/scanner.json`.

## Alur scanner

1. Ambil universe kontrak USDT perpetual yang aktif.
2. Filter likuiditas 24 jam, jumlah transaksi, spread, rentang harga, dan kesegaran quote.
3. Ambil hingga 20 kandidat berdasarkan likuiditas.
4. Evaluasi M15 menggunakan candle tertutup dan confirmed pivots.
5. Ranking kandidat berdasarkan kualitas setup M15, aktivitas, dan likuiditas.
6. Pilih maksimal 1–2 kandidat untuk analisis mendalam.
7. Jalankan validasi strategi, kalender berita, manajemen risiko, dan audit AI.
8. Catat hasil PAPER dan kirim Telegram summary, watchlist, entry valid, atau cancellation sesuai status.

**Penting:** Stage 2 M15 adalah screening kandidat, bukan bukti entry. Evaluator strategi utama tetap menjadi gerbang entry. Tidak ada jaminan jumlah sinyal per siklus.

## Timeframe dan mode

- Auto-scanner utama dikunci ke mode SCALPING: bias/setup M15 dan eksekusi M5.
- Mode INTRADAY dan SWING masih tersedia pada model/dashboard manual, tetapi tidak dipakai oleh auto-scanner utama.
- Hanya candle yang sudah close yang dipakai sebagai konfirmasi.
- Data futures meliputi last, mark, bid/ask, depth, funding, dan spesifikasi kontrak bila endpoint tersedia.

**Batasan strategi saat ini:** Stage 2 M15 sudah memakai confirmed pivots dan skor evidence yang lebih ketat. Namun evaluator entry utama masih merupakan evaluator strategi PAPER yang sudah ada, bukan implementasi lengkap seluruh checklist reversal master prompt. Karena itu, jangan menganggap setiap kandidat Stage 2 sebagai sinyal entry; periksa checklist yang dilaporkan dan gunakan PAPER untuk mengumpulkan hasil.

## Pengaturan JSON

Kode saat ini membaca dari `config/scanner.json`: sumber market, setup timeframe, jumlah candle M15, batas kandidat Stage 2, dan bobot skor. Pastikan total bobot skor tetap 100. Filter likuiditas, interval scan, dan toggle alert lainnya masih dikendalikan oleh UI atau default `ScannerConfig`, bukan seluruhnya dari JSON.

## Instalasi lokal

Python 3.12 direkomendasikan.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Entry point Streamlit adalah `app.py`.

## Secrets

Tambahkan melalui Streamlit Secrets atau environment variables:

```toml
GROQ_API_KEY = "key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "token_bot_Anda"
TELEGRAM_CHAT_ID = "chat_id_tujuan"
PAPER_INITIAL_EQUITY = 1000.0
```

Dapat menggunakan hingga tujuh key Groq melalui `GROQ_API_KEYS` atau `GROQ_API_KEY_1` sampai `GROQ_API_KEY_7`. Jangan pernah menyimpan token ke source code atau commit secrets.

## Worker persisten 24/7

Streamlit Community Cloud tidak menjamin proses background tetap berjalan setelah sesi tidur/restart. Untuk scanner yang selalu aktif, jalankan worker di VPS atau host persisten:

```bash
python worker.py --interval 300
```

Opsional, untuk memasukkan tier likuiditas menengah ke watchlist:

```bash
python worker.py --interval 300 --medium-liquidity-fallback
```

Opsi tersebut hanya memperluas watchlist; tidak melonggarkan syarat entry, validasi data, atau kalender berita. Worker tidak mengirim order Binance.

Database default: `data/paper_history.sqlite3`. Lokasi dapat diubah melalui `FUTURES_HISTORY_DB`. Pastikan disk host persisten dan lakukan backup jurnal sebelum redeploy.

## Telegram

- `MARKET SCANNER REPORT`: ringkasan market yang diperiksa dan jumlah kandidat.
- `REVERSAL WATCHLIST`: kandidat yang masih menunggu konfirmasi.
- `ENTRY VALID`: hanya setelah strategi, risk gate, dan audit AI lolos.
- `SIGNAL CANCELLED`: setup pending yang batal.

Telegram summary bukan sinyal entry. Watchlist bukan instruksi membuka posisi. Tidak ada auto-trading.

## Mode dan keselamatan

- Default: PAPER.
- Live trading: tidak tersedia dalam versi ini.
- Risiko dasar paper: mengikuti konfigurasi scanner dan ledger.
- Tidak ada martingale atau averaging down.
- Jika data market stale, kalender gagal, atau hard gate tidak lolos, sistem harus WAIT/NO TRADE.
- Leverage paper bukan rekomendasi leverage akun nyata. Liquidation safety tidak boleh diklaim terverifikasi jika hanya dihitung secara simulasi.

## Verifikasi

Pemeriksaan lokal versi ini:

- `python -m compileall -q app.py worker.py futures` berhasil.
- 212 tes non-UI lulus.
- Tes UI Streamlit belum dijalankan di lingkungan build ini karena paket Streamlit tidak tersedia di sana; jalankan kembali di environment yang sudah memasang `requirements.txt`.
- Koneksi live Binance, Groq, Telegram, dan deployment Streamlit belum diuji dari hosting pengguna.

Lulus tes unit tidak membuktikan profitabilitas. Jalankan backtest, paper trading, dan evaluasi out-of-sample sebelum mempertimbangkan perubahan risiko.
