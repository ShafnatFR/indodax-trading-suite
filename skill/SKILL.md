---
name: indodax-trading-suite
description: "Use when running Indodax strategy bot + technical analyst (PAPER/LIVE)."
version: 2.0.0
author: Shafnat
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [indodax, crypto, trading-bot, strategy, technical-analysis, paper-trading]
    related_skills: [indodax-trading, crypto-trading-bot-development, indodax-strategy-bot]
---

# Indodax Trading Suite (Strategy Bot + Technical Analyst)

## When to Use

- User minta screening/scan pasar Indodax top volume dengan rekomendasi entry.
- User minta menjalankan/menyetel strategi trading Indodax (paper atau live).
- User minta analisis teknis berkala (technical analyst): kandidat entry, evaluasi
  posisi, rekomendasi HOLD/TAKE PROFIT/CUT LOSS.
- User minta cek status bot, hasil tick, evaluasi simulasi, atau ekstrak pelajaran.

Don't use for: exchange lain tanpa adaptasi; market order; mode LIVE tanpa persetujuan
eksplisit user.

## File dalam paket

```
scripts/strategy_bot.py      # engine utama: tick/status/reset/config/screen (PAPER default)
scripts/analyst_bot.py       # technical analyst READ-ONLY: skor 30 koin + posisi + rekomendasi
scripts/screener.py          # screening sekali jalan
scripts/idx_v2_client.py     # client Trade API V2 (HMAC-SHA256)
scripts/hold_analysis.py     # analisis hold 1 bulan (daily)
scripts/sim_until_5am.sh     # template simulasi sampai deadline
config/indodax_bot_config.json
```

## Setup (di mesin baru)

**WAJIB Python 3.12** — ccxt/pandas/pandas-ta tidak terpasang di Python 3.11 default.

```bash
PY312="/c/Users/<user>/AppData/Local/Programs/Python/Python312/python.exe"   # Windows
# cek: py -0p
"$PY312" -m pip show ccxt pandas pandas-ta || "$PY312" -m pip install ccxt pandas pandas_ta requests
```

Estimasi: **~1-2 menit, ~100-150 MB** (pandas terbesar). Jalankan semua script dengan
`$PY312` (BUKAN `python` polos).

### API key (hanya untuk LIVE)

- Trade API **V2** di https://indodax.com/trade_api → **IP Permission WAJIB diisi** IP
  publik mesin (`curl https://api.ipify.org`). IP rumah dinamis → update kalau berubah.
- Env var: `IDX_KEY` + `IDX_SECRET` (JANGAN hardcode di script/chat).
- Client V2: `idx_v2_client.py` (HMAC-SHA256, header `X-APIKEY` + `Sign`, `timestamp` ms + recvWindow).

## Usage

```bash
cd <dir-bot>   # salin scripts/ + config ke sini

# Screening sekali jalan (tanpa API key):
$PY312 screener.py --top 30 --tf 1h --limit 150

# Bot (PAPER default):
$PY312 strategy_bot.py screen     # ranking + rekomendasi, tanpa eksekusi
$PY312 strategy_bot.py tick       # 1 siklus: exit-check -> screen -> score -> entry
$PY312 strategy_bot.py status     # equity, posisi, PnL, riwayat trade
$PY312 strategy_bot.py reset      # hapus state (mulai baru)
$PY312 strategy_bot.py config     # tampilkan konfigurasi

# Technical analyst (READ-ONLY, tidak pernah order):
$PY312 analyst_bot.py             # JSON: kandidat entry + posisi virtual + rekomendasi

# Analisis hold 1 bulan (daily, tanpa API key):
$PY312 hold_analysis.py
```

### Cron otomatis

```yaml
# Bot tick tiap 4 jam:
schedule: every 4h
prompt: cd <dir> && $PY312 strategy_bot.py tick

# Technical analyst tiap 30 menit (READ-ONLY):
schedule: every 30m
prompt: cd <dir> && $PY312 analyst_bot.py
```

## Konfigurasi (`indodax_bot_config.json`)

| Field | Default | Arti |
|---|---|---|
| checks_per_day | 6 | Target cek per hari (referensi) |
| top_n_volume | 30 | Jumlah pair top volume yang diskrining |
| min_target_pct | 2.0 | Target profit per trade (%) |
| trailing_stop_pct | 4.0 | Trailing stop dari high-water (%) |
| hard_stop_loss_pct | 5.0 | Cut loss keras (%) |
| max_hold_days | 5 | Timeout posisi (hari) |
| risk_per_trade_pct | 10.0 | Ukuran posisi = % equity |
| allow_maintenance | false | Izinkan pair maintenance |
| exclude_symbols | [] | Pair yang dikecualikan |
| mode | PAPER | PAPER / LIVE |
| start_cash_idr | 500000 | Modal awal (PAPER) |
| max_positions | 5 | Maks posisi paralel |
| max_new_positions_per_tick | 1 | Maks entry baru per tick |
| parabolic_threshold_pct | 25.0 | Blokir entry jika >25% di atas MA20 |
| bollinger_upper_avoid | true | Blokir entry di atas Bollinger upper |
| scan_interval_min | 240 | Interval scan (menit) |

## Hasil simulasi nyata & pelajaran (Agustus 2026)

Sim 8,5 jam engine murni (PAPER, modal Rp 500.000): **-3,03%**.

Akar masalah yang ditemukan (penting untuk dipahami sebelum pakai bot ini):
1. **Rasio risk/reward terbalik**: avg win +3,13% vs avg loss -4,56% (rasio 0,69;
   butuh >1 untuk profit). Strategi menang kecil, kalah besar.
2. **Cut-loss slippage**: config -5%, real avg -7,36% (terburuk -11,88%) karena tick
   tiap 9 menit — harga sudah anjlok sebelum dicek.
3. **Overtrading/churn**: HART dibeli 6x, ZKC 4x dalam 8,5 jam → fee 0,6%/siklus
   menumpuk (total fee Rp 6.000 = 1,2% modal).
4. **Trailing 4% terlalu ketat**: 6 dari 8 trailing exit RUGI (avg -3,08%) — profit
   kecil diserahkan kembali.
5. **Hold saat momentum melemah** menunda profit: SKYAI sempat +6,5% tapi exit +1,99%.

Rekomendasi perbaikan (belum diterapkan — eksperimen):
- Target naik ke +6%, cut-loss turun ke -3%, trailing 6% → rasio 2:1.
- Cooldown 2-3 jam sebelum beli ulang koin yang sama.
- Pisahkan exit-check (tiap 1-2 menit) vs entry (cooldown 30-60 menit).

## Common Pitfalls

1. **`python` default = 3.11 tanpa ccxt** → `ModuleNotFoundError`. Selalu `$PY312`.
2. **Indodax `change` di summaries selalu 0** → range harian dari high/low, bukan change.
3. **Format symbol**: ccxt `BTC/IDR`; summaries `btc_idr`; Trade API V2 `BTCIDR`. Jangan tertukar.
4. **Key V1 ≠ V2**: Trade API V2 menolak key V1 (`-1002 invalid credentials`);
   legacy `/tapi` menolak key V2 (`invalid_version_key`).
5. **IP Permission**: error `-2015 Unauthorized IP address` = IP publik berubah →
   update di halaman trade_api. Cek: `curl https://api.ipify.org`.
6. **urllib SSL rusak di host ini** → `get_summaries()` memakai `curl -4` (subprocess).
7. **Minimum order Indodax ~Rp 10.000** per pair dan **fee ~0,3% per sisi** (0,6%
   beli+jual). Modal < Rp 100rb membuat target 2% nyaris tidak berarti.
8. **Bootstrap**: MA/Bollinger butuh ≥20 candle, RSI/Stoch ≥30. Pakai `limit=150-200`.
9. **State file** (`strategy_state.json`) = cash + posisi + high-water. `reset`
   menghapusnya — jangan reset saat posisi LIVE terbuka.
10. **Jangan pernah** mencantumkan API key/secret di skill, script yang dishare, atau chat.
11. **score_symbol mengembalikan tuple** `(score, reasons, pos_range)` — bukan dict.
    **entry_guard mengembalikan tuple** `(ok, reason)`. **screen_top** mengembalikan
    list dict dengan field `key`/`symbol`. **get_ohlcv butuh format `BTC/IDR`** (bukan
    `btc_idr`). Ini penting saat menulis script yang mengimpor strategy_bot.
12. **Analyst harus READ-ONLY**: jangan memanggil `tick()`/`reset()` dari analyst_bot
    agar tidak mengganggu state simulasi yang sedang jalan.

## Live Trade (HANYA dengan persetujuan eksplisit user)

1. Konfirmasi 2x: symbol/side/amount/price, dan risiko (fee 0,3%/sisi, slippage, volatilitas).
2. Set `"mode": "LIVE"` di config + export `IDX_KEY`/`IDX_SECRET`.
3. Mulai dari nominal kecil (test order) sebelum jumlah besar.
4. Permission Withdraw di API key **jangan diaktifkan** kalau tidak perlu.

## Verification Checklist

- [ ] Python 3.12 (`$PY312 -c "import ccxt, pandas, pandas_ta"`) tanpa error
- [ ] `screener.py --top 30 --tf 1h --limit 150` → JSON ranking, skor wajar
- [ ] `strategy_bot.py status` → `mode: PAPER`, equity = start_cash, 0 posisi
- [ ] `strategy_bot.py tick` → `entries` terisi sesuai guard, `screen_count` ≈ 30
- [ ] `analyst_bot.py` → JSON dengan `score` tidak null, `top_entry_candidates` terurut
- [ ] Config `mode` masih PAPER sebelum user minta LIVE
- [ ] Tidak ada key/secret di file skill/script yang dishare
