# Indodax Trading Suite

Paket lengkap bot strategi trading Indodax + technical analyst, siap dipelajari dan
dijalankan oleh Hermes di perangkat lain. **Tanpa API key/secret** — key diisi sendiri
oleh pengguna (lihat Setup).

## Isi paket

```
indodax-trading-suite/
├── README.md
├── scripts/
│   ├── strategy_bot.py     # Engine utama: SCREEN -> SCORE -> ENTRY -> EXIT (PAPER default)
│   ├── analyst_bot.py      # Technical analyst READ-ONLY (skor 30 koin, rekomendasi, posisi)
│   ├── screener.py         # Screening sekali jalan: top-30 + rekomendasi
│   ├── idx_v2_client.py    # Client Trade API V2 (HMAC-SHA256, X-APIKEY + Sign)
│   ├── hold_analysis.py    # Analisis kandidat hold 1 bulan (daily, RSI/EMA/vol)
│   ├── sim_until_5am.sh    # Template simulasi PAPER sampai deadline tertentu
│   └── requirements.txt
├── config/
│   └── indodax_bot_config.json   # Konfigurasi (PAPER, target 2%, risk 10%/posisi)
└── skill/
    └── SKILL.md            # Skill Hermes (instal ke ~/AppData/Local/hermes/skills/)
```

## Konsep strategi

- **SCREEN**: ambil top-30 pair IDR berdasarkan volume 24 jam, filter stablecoin
  (usdt/usdc/dai/busd/tusd/usde) & red-flag (siren/maintenance/suspended).
- **SCORE**: tiap kandidat diskor 0-100 — RSI14 + StochRSI + uptrend (MA20 slope) +
  volume + posisi range harian. **Pullback ke MA20 di uptrend = skor tertinggi**.
- **ENTRY**: beli kandidat skor tertinggi, kecuali parabolic (>25% di atas MA20) atau
  di atas Bollinger upper. Max 1 posisi baru/tick, max 5 posisi.
- **EXIT**: target +2% -> HOLD kalau tren masih naik / SELL kalau momentum melemah.
  Cut loss 5%, trailing 4% dari high-water, timeout 5 hari.

## Setup (di mesin baru)

**Wajib Python 3.12** — ccxt/pandas/pandas-ta tidak ada di Python 3.11 default.

```bash
# cari python 3.12 (Windows):
py -0p

# install dependencies (~1-2 menit, ~100-150 MB):
"C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe" -m pip install ccxt pandas pandas_ta requests
```

Lalu jalankan semua script dengan python 3.12 (BUKAN `python` polos).

### API key (hanya untuk LIVE)

- Buat Trade API **V2** di https://indodax.com/trade_api → **IP Permission WAJIB diisi**
  IP publik mesin (`curl https://api.ipify.org`). IP rumah dinamis → update kalau berubah.
- Env var: `IDX_KEY` + `IDX_SECRET` (JANGAN hardcode di script/chat).
- Client V2: `idx_v2_client.py` (HMAC-SHA256, header `X-APIKEY` + `Sign`, timestamp ms + recvWindow).

## Cara pakai

```bash
cd indodax-trading-suite/scripts

# Screening sekali jalan (tanpa API key):
python3.12 screener.py --top 30 --tf 1h --limit 150

# Bot PAPER (default, aman):
python3.12 strategy_bot.py screen   # ranking + rekomendasi, tanpa eksekusi
python3.12 strategy_bot.py tick     # 1 siklus: exit-check -> screen -> score -> entry
python3.12 strategy_bot.py status   # equity, posisi, PnL, riwayat trade
python3.12 strategy_bot.py reset    # hapus state (mulai baru)
python3.12 strategy_bot.py config   # tampilkan konfigurasi

# Technical analyst (READ-ONLY, tidak pernah order):
python3.12 analyst_bot.py           # JSON: kandidat entry + posisi + rekomendasi

# Analisis hold 1 bulan:
python3.12 hold_analysis.py

# Simulasi sampai deadline (contoh: besok 05:00):
bash sim_until_5am.sh
```

### Cron otomatis (Hermes)

```yaml
# Bot tick tiap 4 jam:
schedule: every 4h
prompt: cd <dir>/scripts && python3.12 strategy_bot.py tick

# Technical analyst tiap 30 menit (READ-ONLY):
schedule: every 30m
prompt: cd <dir>/scripts && python3.12 analyst_bot.py
```

## Konfigurasi (`config/indodax_bot_config.json`)

| Field | Default | Arti |
|---|---|---|
| checks_per_day | 6 | Target cek per hari |
| top_n_volume | 30 | Jumlah pair top volume yang diskrining |
| min_target_pct | 2.0 | Target profit per trade (%) |
| trailing_stop_pct | 4.0 | Trailing stop dari high-water (%) |
| hard_stop_loss_pct | 5.0 | Cut loss keras (%) |
| max_hold_days | 5 | Timeout posisi (hari) |
| risk_per_trade_pct | 10.0 | Ukuran posisi = % equity |
| max_positions | 5 | Maks posisi paralel |
| max_new_positions_per_tick | 1 | Maks entry baru per tick |
| parabolic_threshold_pct | 25.0 | Blokir entry jika >25% di atas MA20 |
| bollinger_upper_avoid | true | Blokir entry di atas Bollinger upper |
| mode | PAPER | PAPER / LIVE |

## Pelajaran dari simulasi nyata (Agustus 2026)

Hasil sim 8,5 jam dengan engine murni: **-3,03%**. Analisis akar masalah:

1. **Rasio risiko/imbalan terbalik**: avg win +3,13% vs avg loss -4,56% (rasio 0,69,
   harus >1 untuk profit). Menang kecil, kalah besar.
2. **Cut-loss slippage**: config -5%, real rata-rata -7,36% (terburuk -11,88%) karena
   tick tiap 9 menit — harga sudah anjlok duluan.
3. **Overtrading/churn**: koin yang sama dibeli berulang (HART 6x, ZKC 4x dalam 8,5
   jam) → fee 0,6%/siklus menumpuk.
4. **Trailing terlalu ketat**: 6 dari 8 trailing exit RUGI (avg -3,08%) — profit kecil
   yang sudah di tangan diserahkan kembali.

Rekomendasi perbaikan (belum diterapkan — silakan eksperimen):
- Target naik ke +6%, cut-loss turun ke -3%, trailing 6% (rasio 2:1)
- Cooldown 2-3 jam sebelum beli ulang koin yang sama
- Pisahkan exit-check (tiap 1-2 menit) vs entry (cooldown 30-60 menit)

## Pitfalls

1. **`python` default = 3.11 tanpa ccxt** → selalu pakai python 3.12.
2. **Indodax `change` di summaries selalu 0** → range harian dari high/low.
3. **Format symbol**: ccxt `BTC/IDR`; summaries `btc_idr`; Trade API V2 `BTCIDR`.
4. **Key V1 ≠ V2**: Trade API V2 menolak key V1 (-1002); /tapi menolak key V2.
5. **IP Permission**: error `-2015 Unauthorized IP` = IP publik berubah → update.
6. **urllib SSL rusak di sebagian host Windows** → `get_summaries()` pakai `curl -4`.
7. **Minimum order Indodax ~Rp 10.000/pair, fee 0,3%/sisi** → modal kecil membuat
   target 2% nyaris tidak berarti.
8. **Bootstrap**: MA/Bollinger butuh ≥20 candle, RSI/Stoch ≥30. Pakai `limit=150-200`.
9. **State file** `strategy_state.json` = cash + posisi + high-water. `reset` menghapusnya.
10. **Jangan pernah** mencantumkan API key/secret di skill, script yang dishare, atau chat.

## Keamanan

- Paket ini TANPA key/secret apa pun. Jangan pernah menambahkannya ke file yang dishare.
- Mode LIVE terkunci (butuh env `INDODAX_LIVE_ENABLED=1` + konfirmasi eksplisit).
- Analisis teknis = data + disiplin, BUKAN jaminan profit. Mulai dari PAPER.
