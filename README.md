# Futures Desk v5.8.2 — scanner awal berbasis spot

Sumber scanner dikembalikan ke **Binance Spot public market data**, seperti pada
`legacy/app_v5_5.py`: `https://data-api.binance.vision` dengan endpoint `/api/v3/`.
Scanner utama, analisis candle, pemeriksaan ulang dan pemantauan paper trading
semuanya memakai spot. Aplikasi utama tidak memerlukan endpoint futures.

Yang dikembalikan adalah **sumber data scanner awal**. Dashboard, penyaringan
maksimal 10 koin, pemilihan 1–2 kandidat, perbandingan tiga mode, Groq, Telegram,
dan paper trading otomatis dari v5.8 tetap dipertahankan. Kode legacy tidak
menjadi entry point. README versi sebelumnya disimpan dalam `legacy/`.

## Memasang

1. Ekspor cadangan jurnal yang sudah ada dari tab **Paper trading** sebelum
   redeploy, terutama bila disk hosting bersifat sementara.
2. Ekstrak ZIP dan salin seluruh isi folder `secretagent-main` ke root proyek.
   Ganti modul `futures/` juga, bukan hanya `app.py`.
3. Pertahankan Secrets Groq dan Telegram. Tidak memerlukan Binance API key.
4. Entry point Streamlit tetap **`app.py`**. Deploy atau reboot aplikasi.
5. Pastikan tampil **FUTURES DESK / 5.8.2 / PAPER / SPOT**, lalu klik
   **Scan sekarang**. Diagnosis koneksi harus menyebut Binance Spot.

ZIP ini merupakan pembaruan kode; belum diterapkan ke hosting Anda.

Untuk lokal dengan Python 3.12:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Aktifkan `.venv` setelah membuatnya: Windows `.venv\Scripts\activate`,
macOS/Linux `source .venv/bin/activate`.

## Arti SPOT REFERENCE

Harga spot dan perpetual dapat berdekatan, tetapi tidak identik. Funding
mendorong kedekatan harga tanpa menjamin selisih selalu kecil. Volume, candle,
spread, depth dan daftar pair spot juga berbeda dari pasar perpetual.

| Bagian | Perilaku versi ini |
| --- | --- |
| Universe | Pair spot USDT aktif, diizinkan untuk spot; pengecualian stablecoin/fiat/emas mengikuti scanner lama. Tidak membuktikan adanya kontrak perpetual untuk tiap pair. |
| Harga dan candle | Last, OHLCV, bid/ask dan depth spot; tidak dicampur dengan futures. |
| LONG/SHORT dan leverage | Model PAPER saja, bukan transaksi spot bermargin atau perpetual riil. |
| Entry dan SL | Simulasi berbasis Last spot. Fill memakai bid/ask dan depth yang diamati. |
| Mark Price dan funding aktual | Tetap `None`/tidak tersedia. Tidak dibuat angka atau jadwal pengganti. |
| Check khusus futures | Deviasi Mark–Last, funding kontrak dan likuidasi berstatus SIMULASI. Check teknis, risiko, likuiditas dan kalender lainnya tetap berlaku. |
| Biaya | Fee, slippage dan cadangan biaya funding adalah asumsi model. Cadangan dibebankan saat entry, bukan pembayaran funding aktual. |
| Tick/lot/notional | Spesifikasi spot dipakai sebagai grid simulasi; filter dinamis order bursa tidak diverifikasi. |
| Usia quote | Spot book/price/depth tidak memberi timestamp event. Umur memakai estimasi awal permintaan yang ditambatkan ke jam bursa, bukan umur transaksi di bursa. |

Label sumber ada pada GUI, prompt AI, rencana tersimpan dan alert Telegram.
Jalur manual `manual_app.py` tetap memakai futures. Data spot tidak boleh
meloloskan evaluasi live.

## Alur dan pengaturan

Seluruh pair spot USDT aktif → maksimum 10 koin → 1–2 kandidat → tiga mode
→ AI → pemeriksaan ulang → jurnal PAPER PENDING → Telegram.

- Auto-scan aktif saat sesi dibuka, default 90 detik. Interval tersedia
  30/45/60/90 detik. Tombol manual juga bekerja saat auto-scan dimatikan.
- Filter awal default: volume quote 10 juta USDT/24 jam, 1.000 transaksi,
  spread maksimal 10 bps dan rentang 24 jam 0,5–30%.
- Ranking kandidat memakai 100 candle 1H tertutup; analisis mendalam
  memerlukan minimal 500 candle tertutup per timeframe.
- SCALPER: bias 1H / sinyal 5M. INTRADAY: 4H / 15M. SWING: 1D / 4H.
- EMA, ADX, pullback, RSI, ATR, volume relatif, struktur dan VWAP UTC tetap
  diperiksa. SWING tidak diwajibkan menggunakan VWAP harian.
- Risiko per trade maksimal 0,5%, total 1%, dua posisi/pending, leverage
  model 3x dan margin 20% per posisi. Seluruh altcoin satu kelompok dengan
  batas risiko 0,5%. Entry baru berhenti pada rugi harian 2% atau tiga rugi beruntun.
- AI dapat menolak, tetapi tidak meloloskan baseline yang gagal. Kalender
  CPI/FOMC tetap wajib tersedia; gagal/di luar cakupan menghasilkan WAIT.
- WAIT/NO TRADE tidak membuka posisi atau mengirim alert. Jumlah kandidat
  tidak menjamin sinyal pada setiap siklus.

`STRATEGY_RULES.txt` asli tidak diubah. Adaptasi PAPER berbasis spot diungkapkan
pada aplikasi dan instruksi tambahan AI.

## Secrets

```toml
GROQ_API_KEY = "key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "token_bot_Anda"
TELEGRAM_CHAT_ID = "chat_id_tujuan"
PAPER_INITIAL_EQUITY = 1000.0
```

Nama lama tetap didukung. Groq dapat memakai `GROQ_API_KEYS` atau
`GROQ_API_KEY_1` sampai `GROQ_API_KEY_7`, maksimum tujuh key unik. Contoh ada
di `.streamlit/secrets.toml.example`. Environment variable dengan nama yang
sama didukung; `.env` tidak otomatis dibaca. Jangan commit secrets asli.
Tanpa Groq yang valid tidak ada sinyal yang disetujui.

## Paper trading dan jurnal

PENDING belum terisi. Sesudah trigger teramati, quote harus masih di dalam
batas limit dan depth harus cukup. Simulator memakai harga limit terburuk
yang diizinkan. SL dipicu Last spot dan memakai fill bid/ask dengan slippage.
TP1 2R sekitar 50%, TP2 3R sekitar 25%, sisanya runner; pecahan mengikuti lot.
Setelah TP1, SL bergeser ke BE termasuk biaya; setelah TP2, trailing 2 ATR.
Time-exit tetap 12 candle tanpa +1R dan batas hari UTC untuk scalper/intraday.
PnL tidak dikalikan leverage lagi.

Pemantauan memakai polling sehingga pergerakan di antara scan dapat terlewat.
Gap lebih dari maksimum(180 detik, tiga interval) membatalkan pending atau
menutup posisi pada quote baru sebagai DATA_GAP_EXIT. Likuidasi, antrean,
partial entry dan funding riil tidak dimodelkan.

Jurnal spot default baru: `data/spot_paper_history.sqlite3`. Jurnal perpetual
lama tidak diubah/dihapus. Saldo dan hasil simulasi spot dimulai terpisah.

- `SPOT_HISTORY_DB` memilih lokasi jurnal spot secara eksplisit.
- Bila hanya `FUTURES_HISTORY_DB` lama terpasang, aplikasi memakai berkas
  saudara dengan akhiran `_spot_reference`: `paper.db` menjadi
  `paper_spot_reference.db`. Lokasi volume persisten tetap digunakan.
- Jangan arahkan `SPOT_HISTORY_DB` ke jurnal perpetual lama. Proteksi sumber
  menolak pemantauan posisi perpetual memakai quote spot.
- CSV berisi 500 trade terbaru; cadangan JSON berisi seluruh trade dan event.
  Simpan cadangan sebelum disk hosting di-reset. Mengubah saldo awal di Secrets
  tidak mereset jurnal yang sudah terbentuk.

## Koneksi dan worker

```bash
python check_market_access.py
python worker.py --once --no-telegram
python worker.py --interval 90
```

Diagnosis hanya mengakses empat endpoint spot publik (`time`, `exchangeInfo`,
`ticker/24hr`, `ticker/bookTicker`), tanpa AI, jurnal atau Telegram. Exit code 0
berarti pemeriksaan awal lulus; 2 berarti gagal. Hasil dari satu mesin tidak
membuktikan akses mesin hosting lain.

HTTP 451 menjeda auto-scan; tombol manual dapat mencoba kembali setelah akses
host diperbaiki. HTTP 418/429 mengikuti cooldown. Tidak ada fallback ke futures,
data sintetis atau perputaran host. Sumber spot tetap bergantung pada kebijakan
akses dan kondisi jaringan penyedia data.

Worker memakai sumber dan jurnal yang sama dengan GUI. Untuk 24/7, jalankan
di host yang terus hidup. Secrets Cloud tidak otomatis diambil dari host lain.
Lock mencegah scan bersamaan pada jurnal yang sama. Jika worker menjadi
scheduler utama, matikan auto-scan GUI. Pengaturan GUI tidak mengubah worker
yang sudah berjalan.

## Pengujian

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Lihat `VALIDATION.md` untuk hasil terbaru dan batas pengujian live. Fixture tes
bukan data pasar dan tidak dipakai sebagai fallback aplikasi.

Sumber teknis:
[Binance Spot public market data](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only),
[filter spot](https://developers.binance.com/en/docs/products/spot/filters),
[funding perpetual](https://www.binance.com/en/support/faq/detail/360033525031).
