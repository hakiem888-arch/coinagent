# Catatan validasi historis v5.8.2 — scanner spot (arsip)

Revisi 26 Oktober 2026 (WIB). Sumber scanner awal diidentifikasi dari
`legacy/app_v5_5.py`, yang menggunakan `data-api.binance.vision` dan `/api/v3/`.
Pemulihan mencakup sumber data, bukan penggantian seluruh aplikasi dengan UI legacy.

## Pemeriksaan revisi ini

- `python -m pytest -q`: **221 tes lulus**, termasuk 19 tes spot baru.
- `python -m compileall -q app.py worker.py check_market_access.py futures tests`: lulus.
- `STRATEGY_RULES.txt` identik byte demi byte dengan ZIP lampiran.
- Seluruh transport HTTP dalam tes diblokir kecuali mock. Tidak ada pesan
  Telegram, permintaan Groq nyata, atau order bursa yang dikirim saat pengujian.

Tes spot memeriksa: endpoint publik spot saja; penolakan endpoint futures/private;
metadata spot tanpa `contractType`; filter NOTIONAL; MARKET_LOT_SIZE nol yang
berarti aturan dinonaktifkan; candle tertutup dan berbagi quote antarmode;
Mark/funding tetap null; timestamp awal request yang tidak menyamarkan koneksi
lambat; penghapusan pair tidak aktif/stable dan quote lama; LONG/SHORT paper;
entry, stop-loss dan PnL bersih; pengecualian SIMULASI terbatas; larangan validasi
live dengan spot; perlindungan jurnal perpetual; deduplikasi alert; konteks AI;
dan alur seluruh market → 10 shortlist → dua kandidat → tiga mode.

Tes dashboard memastikan request awal `/api/v3/time`, label SCANNER SPOT,
tombol manual, jeda HTTP 451 dan pemulihan. Tes worker memastikan adapter spot
dipakai. Tes futures/manual lama tetap lulus.

## Batas pengujian langsung

`python check_market_access.py` dijalankan sekali dari lingkungan pengerjaan.
Koneksi ke `https://data-api.binance.vision/api/v3/time` gagal sekitar 5 detik,
tanpa menerima status HTTP. Ini bukan bukti HTTP 451 dan bukan bukti endpoint
spot gagal dari hosting pengguna. Karena koneksi pertama gagal, endpoint
selanjutnya tidak diuji langsung. Akses Binance, kalender, Groq dan Telegram
dari hosting pengguna belum diverifikasi. Tidak ada klaim hasil trading live.

GUI dan worker memakai jurnal spot baru agar saldo/PnL perpetual lama tidak
berubah akibat pergantian sumber. Custom `FUTURES_HISTORY_DB` dipertahankan
sebagai basis direktori/nama, dengan sufiks `_spot_reference`; variabel
`SPOT_HISTORY_DB` dapat menetapkan jurnal spot secara eksplisit.

## Catatan versi sebelumnya (arsip)

Bagian di bawah adalah laporan dari ZIP asal. Pernyataan tentang sumber futures,
jumlah tes lama dan perilaku v5.8/v5.8.1 tidak menggambarkan sumber default v5.8.2.

# Catatan validasi v5.8.1

Tanggal pengujian: 26 Oktober 2026 WIB.

## Hasil revisi ini

- `python -m pytest tests -q --disable-warnings --maxfail=3`: **202 passed** dalam 18,79 detik.
- Sebelum perubahan, 21 tes terfokus pada dashboard, adapter, dan scanner lulus. Tes tombol manual sudah lulus pada kode asal. Masalah yang ditemukan adalah kegagalan permintaan data dan status UI yang menyesatkan.
- Seluruh tes menggunakan HTTP simulasi. Fixture otomatis melarang HTTP eksternal, sehingga tidak ada panggilan nyata ke AI/Telegram selama pengujian.
- Kompilasi `app.py`, `manual_app.py`, `worker.py`, `check_market_access.py`, modul `futures/`, dan tes berhasil pada Python 3.12.14.
- Startup Streamlit lokal: health HTTP 200 `ok`, halaman utama HTTP 200.
- Dependensi menggunakan versi yang sama dengan ZIP asal, termasuk Streamlit 1.66.1.
- Aturan strategi, implementasi strategi, ledger paper, dan requirements sama byte per byte dengan ZIP yang dilampirkan. Revisi ini mengubah pelaporan, penghentian akibat penolakan akses, dan penjadwalan tampilan.

## Regresi yang diperiksa

1. HTTP 451 pada permintaan pertama tetap menyimpan waktu percobaan, ID scan, status FAILED, endpoint, dan durasi. `now_ms` tetap nol ketika waktu bursa belum tersedia.
2. Data market yang belum termuat tampil sebagai N/A. Peringatan snapshot lama tidak muncul bila belum ada data, termasuk bila endpoint waktu berhasil tetapi exchangeInfo gagal.
3. Klik manual berulang benar-benar memanggil adapter lagi dan menghasilkan ID percobaan baru, termasuk saat auto-scan OFF.
4. Auto-scan tidak mengulang permintaan setelah HTTP 451. Percobaan manual yang berhasil menghapus status penolakan dan memungkinkan scan terjadwal berikutnya.
5. HTTP 451 pada monitoring posisi, screening kandidat, analisis mendalam, atau fundingInfo menghentikan proses. Tidak ada penggantian sumber data atau sinyal baru setelah kegagalan itu.
6. Lock thread/proses memberikan pemberitahuan ketika percobaan baru belum dimulai. Lock tetap dilepas pada jalur kegagalan yang diuji.
7. Worker berhenti dengan exit code 2 setelah HTTP 451. Pemeriksa akses berhenti pada kegagalan pertama dan menolak respons yang strukturnya tidak sesuai.
8. Seluruh tes regresi yang sudah ada pada strategi, kalender, indikator, AI, jurnal, deduplikasi, Telegram, UI manual, dan scanner tetap lulus.

## Batas validasi revisi ini

- `python check_market_access.py` dijalankan sekali dari lingkungan pengerjaan. Permintaan `/fapi/v1/time` gagal pada koneksi dalam sekitar 5 detik. Tidak ada status HTTP yang diterima. Ini berbeda dari HTTP 451 yang dilaporkan pada hosting pengguna.
- Pengujian tidak membuktikan akses API Binance dari host pengguna. Tidak ada akses ke deployment Streamlit, repo GitHub, Secrets, atau jurnal produksi pengguna pada pengerjaan ini.
- Pengujian UI menggunakan Streamlit AppTest. Startup HTTP tidak membuktikan cadence timer pada browser nyata; timer browser belum diuji langsung.
- Tidak dilakukan transaksi riil, pengiriman pesan Telegram nyata, backtest, atau validasi profitabilitas.
- ZIP revisi belum diterapkan ke GitHub/Streamlit. Perbaikan kode tidak mencabut pembatasan wilayah/IP atau kebijakan akses penyedia data.
- Cadangkan jurnal sebelum redeploy jika disk hosting bersifat sementara. SQLite dan Secrets yang sudah ada tidak perlu dihapus.

---

## Catatan historis dari paket v5.8

Bagian berikut berasal dari ZIP yang dilampirkan. Angka dan pemeriksaan eksternal di bawah adalah laporan versi sebelumnya, bukan klaim pengujian baru. Gunakan hasil v5.8.1 di atas untuk menilai revisi ini.

# Catatan validasi v5.8

Tanggal laporan: 26 Oktober 2026 WIB (25 Oktober 2026 UTC).

## Hasil akhir

- `python -m pytest -q --disable-warnings --maxfail=3`: **185 passed** dalam 10,59 detik.
- Kompilasi `app.py`, `manual_app.py`, `worker.py`, dan seluruh modul `futures/` berhasil.
- Startup Streamlit lokal: `/_stcore/health` mengembalikan HTTP 200 `ok`; halaman `/` HTTP 200.
- Dashboard otomatis dan alur manual dibuka dengan Streamlit AppTest tanpa exception pada skenario yang diuji.
- Masterprompt sama persis dengan `STRATEGY_RULES.txt` dalam ZIP v5.6 asli. SHA-256:
  `ceee3193cec00c2ec1d6193e568c18fb26d1647eab7655ad6a3d8e514a562fa5`.
- Tidak ada file Secrets asli dalam paket. Semua HTTP eksternal diblokir otomatis selama test suite; tidak ada pesan Telegram atau order bursa yang dikirim oleh pengujian.

## Scanner otomatis dan GUI

- Universe sintetis 300 kontrak menguji penyaringan seluruh simbol, jenis kontrak, quote/margin USDT, status TRADING, volume, jumlah transaksi, spread, rentang harga, serta timestamp.
- Alur terpadu dengan 120 market sintetis menghasilkan maksimal 10 anggota shortlist, dua kandidat, evaluasi tiga mode pada setiap kandidat, dan maksimal dua ulasan AI. BUY/LONG serta SELL/SHORT tercakup.
- Pilihan satu kandidat, interval 30/45/60/90, auto-scan aktif secara default, stop/manual scan, dan tidak adanya input pair, mode, API key, atau token pada dashboard diperiksa.
- Mode ditentukan oleh hasil aturan; AI dapat menolak. WAIT/NO TRADE, kalender tidak tersedia, data gagal, dan perubahan trigger setelah AI tidak membuka paper trade atau mengirim alert.
- Jurnal PENDING dibuat sebelum transport Telegram dipanggil. Risiko portofolio diperiksa ulang untuk kandidat berikutnya.
- Lock thread/proses dan jadwal bersama mencegah siklus tumpang tindih pada database yang sama. Lock dilepas saat error pasar maupun kegagalan penulisan ringkasan.
- Kandidat yang tertunda karena siklus lambat diprioritaskan pada siklus berikutnya apabila masih terpilih.
- Ringkasan worker, pesan error, jurnal dan ekspor diuji. `worker.py --once` diuji dengan transport simulasi; log tidak berisi kredensial.

## Data, indikator, strategi, dan AI

- OHLCV sintetis menguji EMA, RMA/Wilder, RSI 0/50/100, ATR/ADX, volume relatif tanpa candle sinyal dalam baseline, VWAP UTC, dan pivot yang membutuhkan dua candle kanan. Indikator historis tidak berubah akibat data masa depan.
- Setup LONG/SHORT lengkap menguji semua mode timeframe, minimum candle, candle tertutup, gap data, NaN, timestamp masa depan, serta batas snapshot 30 detik.
- Gate spread, Mark–Last, depth, funding, biaya, leverage, isolated, margin, tick/lot, min notional, jarak SL, RR neto, target, batas risiko, dan trigger yang sudah tersentuh tercakup.
- Masterprompt asli dikirim utuh ke Groq. Rotasi maksimal tujuh key, duplikat, autentikasi, timeout/5xx, 429 organisasi, Retry-After, schema JSON, output terpotong/malformed, NaN, harga yang tidak diizinkan, dan arah AI yang salah diuji.
- AI tidak dapat meloloskan gate numerik yang gagal. Perubahan ID rencana setelah respons AI menghasilkan WAIT.
- Tiga mode berbagi download timeframe dan satu batch quote segar. Cache metadata/candle dibatasi, berganti saat boundary candle, dan tidak berubah saat DataFrame pemanggil dimutasi. Harga/candle berjalan tetap diminta ulang.
- Adapter memakai endpoint futures dan tidak beralih ke spot saat akses Binance gagal. Pengukuran request pada fixture bukan benchmark CPU hosting.

## Kalender berita

- Parser CPI menguji ICS, line folding, zona Eastern dan DST; parser FOMC menguji rentang tanggal termasuk lintas bulan, kelengkapan tahun, serta blok seluruh hari rapat.
- Batas jendela CPI/FOMC, cache, sumber gagal, format rusak, kalender kedaluwarsa dan cakupan tanggal hilang diuji. Data kalender tidak lengkap menghasilkan WAIT.
- Di luar test suite, dokumen BLS dan Federal Reserve berhasil diunduh dengan HTTP 200 dan diproses menjadi 80 jendela peristiwa dari cakupan kalender sumber. Ini memverifikasi format sumber saat diperiksa, bukan menjamin jadwal tidak akan berubah atau semua berita mendadak tercakup.

## Paper trading dan Telegram

- Scope PAPER terlihat pada hasil, rencana dan alert. Likuidasi kosong ditandai SIMULASI; aturan manual tetap menuntut verifikasi likuidasi. Gate berita/teknis lain tidak dikecualikan.
- PENDING, trigger, fill virtual LONG/SHORT, quote di luar limit, kedalaman kurang, berita, expiry, perubahan likuiditas, SL sebelum fill, data lama, dan restart jurnal diuji.
- SL/gap, biaya entry/funding/exit, PnL neto, TP1/TP2, kuantitas sisa, BE, runner, dan tidak adanya exit/TP ganda tercakup. Sentuhan candle historis tidak dipakai untuk mengarang fill.
- Tidak ada trade duplikat per candle; satu posisi/pending per simbol, dua posisi/pending total, margin, total risiko, dan risiko grup altcoin diperiksa atomik.
- Dashboard dengan timestamp lama tidak dapat memundurkan hari risiko yang sudah dicatat worker atau menghapus patokan drawdown/rugi beruntun hari berjalan.
- Jeda data panjang membatalkan pending atau menutup posisi pada quote baru dengan label DATA_GAP_EXIT; tidak dibuat urutan harga selama offline.
- Telegram menguji label SCALPER/INTRADAY/SWING, WAIT/NO TRADE diam, AI salah/gagal, expiry/data lama, deduplikasi lintas instance, restart, 429/cooldown, penolakan, dan timeout tanpa retry duplikat.
- Retry pending memerlukan pemeriksaan ulang rencana dan tidak membuat trade kedua. Kegagalan Telegram tidak menghapus jurnal.
- Seluruh uji pengiriman menggunakan transport simulasi; tidak ada pesan test ke pengguna.

## Batas validasi

- Permintaan langsung ke endpoint waktu Binance dari lingkungan pengerjaan mengalami **ReadTimeout**. Pengambilan universe dan data pasar nyata belum berhasil diverifikasi dari lingkungan ini; perlu diverifikasi pada deployment pengguna.
- Key Groq serta token/chat ID Telegram berada di Secrets hosting pengguna dan tidak tersedia di workspace. Integrasi nyata Groq/Telegram belum diuji. Tes hanya membuktikan perilaku kode dengan respons simulasi.
- Streamlit AppTest dan startup HTTP tidak membuktikan cadence timer pada browser nyata. Pengujian browser headless belum dilakukan; unduhan Chromium pada revisi sebelumnya gagal.
- Worker belum dipasang atau dijalankan terus-menerus pada server pengguna. File lock diuji pada Linux; cabang Windows belum diuji langsung.
- Belum ada benchmark CPU pada Community Cloud, forward test, backtest historis, pengukuran expectancy/profit factor/drawdown strategi, atau pembuktian profitabilitas. Seluruh fixture algoritme bersifat sintetis dan tidak digunakan sebagai fallback pasar produksi.
- Paper fill menggunakan polling dan asumsi biaya. Likuidasi, funding aktual sepanjang horizon, antrean order, partial entry, serta urutan tick di antara polling tidak dimodelkan. Ledger tidak disinkronkan dengan akun bursa.
- SQLite memerlukan disk persisten agar bertahan dari reset/redeploy. Cadangan JSON adalah arsip audit; impor lewat GUI belum tersedia.
- Paket ini belum diterapkan ke deployment. Pembaruan tidak menghapus throttle CPU yang sudah berlaku. Timer Streamlit memerlukan sesi aktif; operasional tanpa browser memerlukan worker pada host yang terus hidup.

Status: **185 tes perangkat lunak dan startup HTTP lokal lolos**. Integrasi API nyata, browser timer, performa produksi, serta validasi hasil trading masih belum diverifikasi.


## v5.9.1 multi-provider update

- Added a read-only Bybit USDT linear perpetual adapter and strict provider router. Binance remains primary; failover occurs only on HTTP 451.
- Spot is never used as a futures substitute. Provider identity is carried into snapshots and instrument-kind guards prevent existing Binance paper positions from being marked with Bybit prices (and vice versa).
- Dashboard's stale Binance Spot HTTP 451 wording and stale Spot source caption were corrected.
- `check_market_access.py` now probes Binance Futures first and Bybit public linear-perpetual data second.
- Validation in this build environment: 214 non-UI tests passed. Streamlit UI tests were not run because Streamlit is not installed in the build environment. No live connectivity test to Binance/Bybit, Streamlit hosting test, Groq test, or Telegram delivery test was performed.
- PAPER only. This is not approval for live execution; verify provider access, symbol coverage, quote freshness, funding metadata, contract filters, and price divergence on the actual deployment host before relying on alerts.
