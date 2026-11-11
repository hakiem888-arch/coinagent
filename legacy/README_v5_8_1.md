# Futures Desk v5.8.1: seluruh market, AI, Telegram, paper trading

## Perbaikan HTTP 451 dan status tombol scan

**HTTP 451 adalah penolakan akses data dari jalur jaringan server aplikasi.**
Pada kasus ini scanner gagal pada permintaan waktu bursa sebelum memuat daftar
market. Lokasi/IP server hosting dapat berbeda dari lokasi pengguna yang
membuka browser. Kode HTTP ini saja tidak mengidentifikasi wilayah hosting
atau pihak yang memblokir; periksa dengan Binance/penyedia hosting.

Versi sebelumnya baru mencatat waktu setelah Binance mengirim respons sukses.
Akibatnya percobaan yang gagal terlihat sebagai `Belum scan`, seluruh angka
terlihat nol, dan peringatan snapshot lama muncul walau belum ada data.
Tombol manual tetap memanggil scanner pada pengujian, tetapi respons gagal
yang sama membuat halaman tampak tidak berubah.

Perubahan v5.8.1:

- Setiap percobaan memiliki ID, pemicu MANUAL/AUTO, waktu mulai/selesai, dan
  status. Timestamp percobaan memakai jam server aplikasi; timestamp data
  tetap memakai jam bursa. Keduanya tidak dicampur.
- Data market yang belum berhasil dimuat tampil sebagai `N/A`. Peringatan
  snapshot lama hanya berlaku setelah ada data market.
- HTTP 451 menampilkan diagnosis dan menjeda auto-scan. Tombol **Scan sekarang**
  mencoba kembali, termasuk saat auto-scan OFF. Jika berhasil, auto-scan
  kembali berjalan selama toggle tetap ON.
- Satu timer memperbarui tampilan setiap 5 detik. Jadwal permintaan market
  tetap mengikuti interval 30/45/60/90 detik, tanpa antrean scan susulan.
- Scan yang ditahan oleh lock menampilkan pesan bahwa percobaan baru belum
  dimulai. Worker berhenti dengan exit code 2 ketika menerima HTTP 451.
- `check_market_access.py` memeriksa empat endpoint awal dari mesin tempat
  perintah dijalankan, tanpa memakai Secrets, AI, jurnal, atau Telegram.

**Pembaruan kode ini tidak menghapus pembatasan akses hosting.** Menambah key
Binance, mengganti key Groq/Telegram, atau menekan scan berulang tidak mengubah
kebijakan akses penyedia data. Endpoint tetap Binance USD-M Futures. Tidak
ada penggantian otomatis ke spot, testnet, data sintetis, proxy, atau bursa lain.

### Langkah pemulihan

1. Unduh cadangan jurnal dari tab **Paper trading** sebelum melakukan redeploy
   bila sudah ada transaksi virtual. Simpan Secrets yang ada.
2. Terapkan isi folder `secretagent-main` ke root repo GitHub yang dipakai
   Streamlit. Pastikan entry point tetap `app.py`, lalu deploy/reboot aplikasi.
   Versi yang terlihat harus **FUTURES DESK / 5.8.1 / PAPER**.
3. Klik **Scan sekarang**. Buka **Diagnostik scan dan koneksi** untuk melihat
   ID percobaan, HTTP status, dan endpoint yang gagal dari host Streamlit.
4. Jika masih HTTP 451, konfirmasikan kelayakan akses host kepada Binance atau
   penyedia hosting. Jalankan aplikasi pada lingkungan yang memang diizinkan
   mengakses layanan tersebut. Jika penolakan keliru, minta peninjauan kepada
   penyedia layanan. Reboot sendiri tidak menjamin akses berubah.
5. Bila Anda mengelola mesin tempat aplikasi berjalan, jalankan:

   ```bash
   python check_market_access.py
   ```

   Exit code 0 berarti empat endpoint awal lolos. Exit code 2 berarti ada
   kegagalan, beserta rincian pada keluaran JSON. Uji dari laptop hanya
   membuktikan akses laptop, bukan akses server Streamlit Cloud.
6. Setelah akses diperbaiki, klik **Scan sekarang** atau mulai ulang worker.
   Market dapat dimuat tanpa key Binance. Persetujuan AI tetap memerlukan
   Groq yang valid; sinyal juga harus lolos kalender dan seluruh aturan strategi.

Dasar teknis: [RFC 7725, HTTP 451](https://www.rfc-editor.org/rfc/rfc7725),
[Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment),
dan [reboot Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/reboot-your-app).

Aplikasi utama **tidak memerlukan input pair atau pilihan mode trading**. Saat
sesi dibuka, auto-scan aktif dengan interval awal 90 detik. Seluruh kontrak
**Binance USD-M USDT perpetual berstatus TRADING** diperiksa; spot, COIN-M,
USDC-margined, dan kontrak nonperpetual tidak dicampur dengan strategi linear ini.

Alur: seluruh market → maksimal 10 koin → 1–2 kandidat → perbandingan tiga mode
→ ulasan Groq → pemeriksaan ulang → jurnal PAPER PENDING → alert Telegram.
Tidak wajib ada 10 koin atau sinyal pada setiap siklus. WAIT/NO TRADE tidak
mengirim alert dan tidak membuka paper trade.

## Memasang pembaruan

1. Ekstrak ZIP. Ganti isi repo dengan isi folder `secretagent-main`, termasuk
   seluruh folder `futures/`, `app.py`, `worker.py`, `STRATEGY_RULES.txt`, dan
   `requirements.txt`. Jangan hanya mengganti `app.py`.
2. Pertahankan Secrets Streamlit yang sudah ada. Tidak ada API key, token bot,
   atau chat ID yang perlu dimasukkan pada GUI.
3. Main file Streamlit: **`app.py`**. Setelah deploy, pastikan halaman berjudul
   **Futures Desk / 5.8.1 / PAPER**. Auto-scan aktif; interval dapat diubah menjadi
   **30/45/60/90 detik** dan kandidat mendalam dapat dibatasi satu atau dua.
4. Semua asumsi awal sudah diisi. Saldo awal virtual 1.000 USDT; bukan pembacaan
   saldo Binance. Tombol **Scan sekarang** tersedia dan toggle auto-scan dapat
   dimatikan. Tidak ada order riil yang dikirim.

ZIP ini belum otomatis diterapkan ke hosting. Python 3.12 digunakan dalam
pengujian. Untuk lokal:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Aktifkan `.venv` sebelum instalasi/jalankan perintah: Windows
`.venv\Scripts\activate`; macOS/Linux `source .venv/bin/activate`.

## Secrets

Nama yang sudah dipakai tetap didukung:

```toml
GROQ_API_KEY = "key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "token_bot_Anda"
TELEGRAM_CHAT_ID = "chat_id_tujuan"
PAPER_INITIAL_EQUITY = 1000.0
```

Beberapa key dapat memakai `GROQ_API_KEYS = ["key1", "key2"]` atau
`GROQ_API_KEY_1` sampai `GROQ_API_KEY_7`, maksimum tujuh key unik. Bot harus
sudah dapat mengirim ke chat tujuan. Model Groq harus mendukung schema JSON
strict. File contoh ada pada `.streamlit/secrets.toml.example`.

`PAPER_INITIAL_EQUITY` hanya dipakai ketika jurnal pertama kali dibuat. Mengubah
Secrets tidak menghapus trade atau mengganti saldo jurnal yang sudah ada.
Environment variable bernama sama didukung; file `.env` tidak otomatis dibaca.
Jangan commit `.streamlit/secrets.toml` asli.

## Cara pemilihan otomatis

| Tahap | Data dan aturan |
| --- | --- |
| Seluruh market | `exchangeInfo`, ticker 24 jam seluruh simbol, dan book ticker seluruh simbol; dipadankan dengan kontrak aktif USDT perpetual. |
| Shortlist maksimal 10 | Default volume quote ≥10 juta USDT/24 jam, transaksi ≥1.000, spread ≤10 bps, rentang 24 jam 0,5–30%, timestamp awal ≤60 detik. Minimum volume dapat diubah. Prioritas likuiditas memakai log volume/jumlah transaksi dan penalti spread. |
| Pilih 1–2 kandidat | Pada setiap anggota shortlist, 100 candle 1H tertutup dipakai untuk ranking aktivitas volume dan besar pergerakan enam jam relatif terhadap rata-rata true range. Arah ranking netral; ini bukan sinyal entry. |
| Analisis mendalam | Setiap kandidat diperiksa pada ketiga mode di bawah, dengan ≥500 candle tertutup per timeframe. Semua indikator/ambang teknis dari masterprompt diterapkan. |
| Pilih mode | Dahulukan setup VALID, kemudian kelayakan RR bersih, kekuatan ADX, dan volume relatif. Jika semua mode gagal, tidak ada paksaan menghasilkan trade. Skor bukan probabilitas menang. |
| AI | Maksimal satu ulasan per kandidat, hingga dua per siklus. Groq menerima masterprompt asli utuh, kriteria, rencana mode terpilih, dan ringkasan perbandingan ketiga mode. AI dapat memveto; tidak mengubah harga, ukuran, atau meloloskan filter wajib yang gagal. |
| Pemeriksaan ulang | Harga/order book/candle berjalan dan anggaran portofolio diperiksa kembali setelah AI. Perubahan ID rencana, trigger yang sudah tersentuh, data lama, atau kapasitas portofolio habis membatalkan persetujuan. |
| Jurnal/alert | Sinyal disimpan atomik sebagai PENDING sebelum Telegram dikirim. Kegagalan Telegram tidak menghilangkan jurnal. WAIT/NO TRADE tidak dikirim. |

| Jenis pada alert | Bias tren | Eksekusi/sinyal |
| --- | --- | --- |
| SCALPER | 1H | 5M |
| INTRADAY | 4H | 15M |
| SWING | 1D | 4H |

Indikator mendalam: EMA 50/200 dan ADX 14 untuk bias; EMA 20/50, RSI 14, ATR 14,
volume relatif, struktur/pivot terkonfirmasi, serta VWAP reset 00.00 UTC untuk
scalping/intraday. SWING tidak diwajibkan memakai VWAP harian. Ranking 100 candle
awal tidak menggantikan pemanasan minimum 500 candle pada analisis strategi.

Data funding, spread, deviasi Mark–Last, depth, tick/lot, jarak SL, ruang target,
biaya, leverage, dan batas risiko tetap diperiksa. Satu pair hanya dapat memiliki
satu posisi/pending paper. Dua kandidat tidak berarti dua trade pasti dibuka.

## Masterprompt dan adaptasi PAPER

`STRATEGY_RULES.txt` tetap utuh dari lampiran awal. Aturan numerik strategi tidak
diubah untuk memaksakan sinyal. Perubahan dari alur manual menjadi paper otomatis
memerlukan konteks simulasi yang **ditampilkan dalam GUI, prompt AI, jurnal, dan
alert**:

- Ekuitas, posisi, margin bebas, risiko terpakai, unrealized PnL, dan rugi beruntun
  berasal dari ledger virtual, bukan akun riil. Tidak diperlukan Binance API key.
- Fee, slippage, dan cadangan funding adalah asumsi simulator. Nilai awal fee
  0,05%, slippage exit 2 bps, dan total cadangan funding 0,03% dapat diubah.
  Cadangan adalah biaya model, bukan prediksi funding. Untuk SWING khususnya,
  kecukupan cadangan sepanjang horizon tetap merupakan keterbatasan simulasi.
- **Likuidasi bursa tidak dimodelkan.** Harga likuidasi tidak dibuat-buat; check
  tersebut ditandai `SIMULASI`, bukan “terverifikasi”. Pengecualian ini hanya
  berlaku pada scope PAPER. Mesin manual tetap menuntut verifikasi likuidasi.
- Maksimum risiko per rencana 0,5% ekuitas, risiko gabungan 1%, dua posisi/pending,
  leverage maksimum 3x, dan margin maksimum 20% per posisi tetap diterapkan.
  Seluruh altcoin diperlakukan satu kelompok dengan batas 0,5% secara konservatif,
  termasuk ketika arahnya berbeda. Tidak ada martingale atau averaging down.
- Entry baru berhenti pada drawdown harian model ≥2% atau tiga trade rugi beruntun
  pada hari UTC berjalan. Saldo/margin dapat tetap berfluktuasi dari posisi terbuka.

Alert bertajuk **PAPER | BUY / LONG** atau **PAPER | SELL / SHORT**, diikuti jenis
SCALPER/INTRADAY/SWING. Ini keputusan simulasi bersyarat, bukan persetujuan untuk
menempatkan order akun riil. Tidak ada klaim confidence/win rate atau keuntungan.

## Kalender berita otomatis

CPI dibaca dari iCalendar resmi BLS; FOMC dari halaman jadwal Federal Reserve.
CPI memakai timezone Eastern dengan DST yang benar dan jendela ±15 menit.
Halaman FOMC memberi tanggal rapat; aplikasi memblokir **seluruh hari rapat**
secara konservatif, bukan mengarang jam keputusan/konferensi pers. Ini lebih
ketat daripada jendela minimum pada masterprompt.

Kalender diperbarui tiap 15 menit. Kegagalan unduh, perubahan format, cakupan
tahun/tanggal tidak lengkap, atau kalender kedaluwarsa menghasilkan WAIT.
Pending dibatalkan jika jendela berita belum aman; posisi yang sudah terbuka
tetap dipantau untuk proteksi. Peristiwa tak terjadwal tidak dijamin tercakup.
Kalender tidak diberi tanda “bebas berita” hanya karena sumber gagal diakses.

## Paper trading otomatis

**PENDING** berarti rencana dicatat, belum dianggap terisi. Trigger mengikuti
Last. Setelah trigger teramati, harga ask (LONG) / bid (SHORT) harus masih di
dalam batas stop-limit dan depth harus memadai. Simulator mengisi jumlah penuh
pada **harga limit terburuk yang diizinkan**. Harga yang melompati limit tidak
dikejar dengan market order. Fill historis tidak disimpulkan dari high/low candle
sebelum rencana dibuat. Entry parsial/antrean order bursa tidak dimodelkan.

**OPEN** memiliki kuantitas terisi dan SL virtual. Fee entry dan seluruh cadangan
funding model dibebankan sekali saat entry. Pemantauan berikutnya menangani:

- SL dipicu oleh Mark; fill exit memakai bid/ask dan slippage dengan harga gap yang
  lebih buruk bila terjadi. Jika SL dan target bersamaan pada satu pengamatan,
  SL diprioritaskan.
- TP1 2R untuk sekitar 50%, TP2 3R sekitar 25%, sisanya runner; pecahan mengikuti
  pembulatan lot. TP membutuhkan quote dan depth yang teramati.
- Setelah TP1 terisi, SL sisa bergeser ke estimasi BE termasuk biaya. Setelah TP2,
  trailing 2 ATR memakai candle lengkap setelah TP2, hanya diperketat.
- Time-exit setelah 12 candle tanpa +1R; scalper/intraday keluar paling lambat
  akhir hari UTC, pada quote pertama yang dapat diamati setelah batas waktu.
- Pending kedaluwarsa atau filter bias/biaya/likuiditas/berita yang berubah
  menghasilkan CANCELLED. Harga mencapai SL sebelum entry juga membatalkannya.

**CLOSED** menyimpan PnL virtual neto. PnL dihitung dari kuantitas dan perubahan
harga, dikurangi fee dan biaya model; tidak dikalikan leverage lagi. Nilai
mengambang juga mengurangi estimasi biaya exit. Semua peristiwa SIGNAL, ENTRY,
TP, EXIT, dan pembatalan dicatat dengan waktu.

Pemantauan bersifat polling sehingga pergerakan di antara scan dapat terlewat.
Jika jeda data melebihi maksimum(180 detik, tiga interval scan), pending dibatalkan
atau posisi terbuka ditutup pada quote baru dengan label **DATA_GAP_EXIT** dan
kualitas **DATA_GAP**. Aplikasi tidak menciptakan urutan TP/SL saat offline;
PnL trade tersebut tidak boleh dianggap hasil pemantauan kontinu. Likuidasi,
funding aktual, latensi/antrean order, dan partial entry riil tidak dimodelkan.

Tab Paper trading menampilkan 500 trade terbaru dan ekspor CSV. Cadangan JSON
memuat **seluruh jurnal dan event**. Data SQLite berada di
`data/futures_history.sqlite3` atau path `FUTURES_HISTORY_DB`. Disk ephemeral
hosting dapat terhapus saat redeploy/reset: ekspor cadangan atau gunakan volume
persisten. Cadangan JSON adalah arsip audit; impor/restore lewat GUI belum ada.

## Telegram dan deduplikasi

Satu candle per pair/mode/arah hanya dicatat sekali. Pengiriman juga memiliki
reservasi atomik per bot/chat. Analisis ulang, beberapa tab, serta perubahan
kuantitas tidak mengulang alert candle yang sama. Telegram 429 mengikuti
`retry_after`; pending yang masih valid diperiksa ulang sebelum retry tanpa
membuat trade duplikat. Timeout/hasil pengiriman tidak pasti tidak diulang agar
pesan yang mungkin sudah diterima tidak digandakan. Status pengiriman ada pada
jurnal. Jika deduplikasi/DB tidak tersedia, alert ditahan.

Deduplikasi bertahan selama database masih ada. Salinan aplikasi pada disk/DB
berbeda tidak berbagi deduplikasi. Groq 429 menjeda seluruh pool; key tambahan
bukan cara melewati limit organisasi. Tidak ada pesan test yang dikirim otomatis.

## Sumber daya dan mode 24/7

Cache dibatasi 128 item. Metadata bursa TTL 5 menit, fundingInfo 60 detik, dan
candle tertutup sampai batas candle baru dengan TTL maksimum 5 menit. Candle
berjalan dan harga tidak dicache. Analisis tiga mode satu pair berbagi snapshot
quote serta download timeframe yang sama. Grafik mati secara default; timer
lima detik hanya memperbarui keterangan usia hasil.

Jadwal 30/45/60/90 detik adalah target, bukan jaminan ketika API/hosting lambat.
Siklus tidak ditumpuk. Jika pekerjaan kandidat pertama telah melewati 60 detik,
kandidat berikutnya ditunda dan diprioritaskan pada siklus baru bila masih
terpilih. Request yang sedang berjalan tetap tunduk pada timeout jaringan.
Interval awal 90 detik dipilih untuk mengurangi beban dibanding memindai ratusan
pair dengan seluruh indikator sekaligus. Pembatas CPU platform tetap berlaku.

**Streamlit:** auto-scan berjalan selama sesi aktif. Tab ditutup, koneksi putus,
atau aplikasi tidur menghentikan timer. Tidak ada trik keep-alive untuk menghindari
kebijakan platform.

**Server yang selalu hidup:** jalankan worker opsional, dengan Python/dependensi,
Secrets lokal atau environment, dan disk persisten di mesin tersebut:

```bash
python worker.py --interval 90 --candidates 2
```

Uji satu siklus tanpa mengirim Telegram:

```bash
python worker.py --once --no-telegram
```

Hentikan dengan Ctrl+C. Worker memakai logika/jurnal yang sama; OS file lock dan
jadwal tersimpan mencegah scan tumpang tindih pada DB yang sama. GUI dapat membaca
ringkasan worker dan jurnal. `worker.py` tidak membuat hosting 24/7 sendiri dan
belum dijalankan untuk pengguna. Secrets Streamlit Cloud tidak otomatis tersedia
di server lain; isi lewat file lokal yang tidak di-commit atau environment di
server tersebut. Jalankan sebagai aplikasi pribadi, bukan dashboard publik
multiakun.

Untuk memakai worker sebagai satu-satunya scheduler, matikan toggle auto-scan
GUI. Worker memakai default asumsi `ScannerConfig` dan opsi CLI di atas;
perubahan kontrol biaya pada GUI tidak mengubah konfigurasi worker yang sudah
berjalan. Pengaturan worker dapat diubah pada konstruksi `ScannerConfig` di
`worker.py`, lalu proses dijalankan ulang. Jurnal yang ada tetap dipertahankan.

## Pengujian dan berkas

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Lihat `VALIDATION.md` untuk hasil dan keterbatasan. Transport API pada tes
disimulasikan; fixture bukan data pasar dan tidak digunakan sebagai fallback.
Akses Binance gagal menghasilkan WAIT; aplikasi tidak beralih diam-diam ke spot.

- `app.py`: dashboard otomatis v5.8.1, entry point utama.
- `check_market_access.py`: diagnosis akses endpoint awal dari host yang menjalankannya.
- `worker.py`: scanner tanpa browser pada host yang terus hidup.
- `futures/market_scanner.py`, `screener.py`: alur market, ranking, mode, AI.
- `futures/paper.py`: saldo virtual, lifecycle, jurnal, ekspor.
- `futures/news.py`: kalender resmi dan filter berita.
- `futures/strategy.py`, `indicators.py`, `lifecycle.py`: aturan dan perhitungan.
- `futures/market.py`: data publik futures dan cache terbatas.
- `futures/groq_rotation.py`, `telegram.py`: AI dan alert.
- `manual_app.py`: alur manual v5.7 dengan verifikasi akun/likuidasi, bukan entry point otomatis.
- `legacy/`: arsip v5.5, tidak dieksekusi oleh aplikasi utama.
- `STRATEGY_RULES.txt`: masterprompt asli tanpa perubahan.

Dokumentasi/sumber resmi yang diperiksa pada revisi 26 Oktober 2026:
[Binance market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data),
[BLS kalender](https://www.bls.gov/schedule/),
[Federal Reserve FOMC](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm),
[Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment),
[Groq structured outputs](https://console.groq.com/docs/structured-outputs),
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage).
