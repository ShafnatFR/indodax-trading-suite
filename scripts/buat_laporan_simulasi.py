# -*- coding: utf-8 -*-
"""Generate laporan simulasi PAPER Indodax 31 Agu - 1 Sep 2026 (sampai 05:00 WIB)."""
import json, os, sys, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = r'C:\Users\shafnats\Development\indodax-bot'
st = json.load(open(os.path.join(BASE, 'strategy_state.json'), encoding='utf-8'))

def wib(ts):
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).strftime('%d/%m %H:%M')
    except Exception:
        return ts

def rp(x):
    return 'Rp {:,.0f}'.format(x).replace(',', '.')

trades = st['trades']
positions = st['positions']
ticks = st.get('ticks', [])
start_cash = st['start_cash']
cash = st['cash']

# ---- fetch harga sekarang untuk posisi terbuka (public API) ----
def fetch_tickers(symbols):
    out = {}
    hdr = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
    for sym in symbols:
        ns = sym.replace('/', '').lower()
        try:
            req = urllib.request.Request('https://indodax.com/api/ticker/' + ns, headers=hdr)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            out[sym] = float(d['ticker']['last'])
        except Exception:
            out[sym] = None
    return out

open_syms = [p['symbol'] for p in positions]
prices = fetch_tickers(open_syms)

# ---- agregat per koin dari trade realisasi ----
per_coin = defaultdict(lambda: {'buy': 0.0, 'sell': 0.0, 'n_buy': 0, 'n_sell': 0,
                                'pnl': 0.0, 'wins': 0, 'losses': 0})
for t in trades:
    c = per_coin[t['symbol']]
    if t['side'] == 'BUY':
        c['buy'] += t.get('idr', 0)
        c['n_buy'] += 1
    else:
        c['sell'] += t.get('net_idr', 0)
        c['n_sell'] += 1
        p = t.get('realized_pnl_idr', 0)
        c['pnl'] += p
        if p >= 0:
            c['wins'] += 1
        else:
            c['losses'] += 1

# ---- posisi terbuka: unrealized ----
unreal = {}
for p in positions:
    sym = p['symbol']
    px = prices.get(sym)
    if px:
        unreal[sym] = (px / p['entry_price'] - 1) * 100
    else:
        unreal[sym] = None

total_realized = sum(t.get('realized_pnl_idr', 0) for t in trades if t['side'] == 'SELL')
n_trades = len(trades)
n_sells = sum(1 for t in trades if t['side'] == 'SELL')
wins = sum(1 for t in trades if t['side'] == 'SELL' and t.get('realized_pnl_idr', 0) >= 0)
losses = n_sells - wins
win_rate = (wins / n_sells * 100) if n_sells else 0

avg_win = sum(t.get('realized_pnl_idr', 0) for t in trades if t['side'] == 'SELL' and t.get('realized_pnl_idr', 0) > 0) / wins if wins else 0
avg_loss = sum(t.get('realized_pnl_idr', 0) for t in trades if t['side'] == 'SELL' and t.get('realized_pnl_idr', 0) < 0) / losses if losses else 0

eq_final = ticks[-1]['equity'] if ticks else (cash + sum(p['invested'] for p in positions))
pnl_total = eq_final - start_cash
pnl_pct = (eq_final / start_cash - 1) * 100

# posisi terbuka nilai
open_val = sum(p['invested'] for p in positions)

# ---- build markdown ----
L = []
L.append('# Laporan Simulasi Trading Indodax (PAPER)')
L.append('')
L.append('**Periode:** 31 Agustus 2026 23:28 WIB s/d 1 September 2026 05:00 WIB (34 tick, interval ±9 menit)')
L.append('')
L.append('**Mode:** PAPER (uang virtual) — tidak ada dana asli yang dipertaruhkan')
L.append('')
L.append('## Ringkasan')
L.append('')
L.append('| Metrik | Nilai |')
L.append('|---|---|')
L.append(f'| Modal awal | {rp(start_cash)} |')
L.append(f'| Equity akhir | {rp(eq_final)} |')
L.append(f'| PnL total (realisasi + unrealized) | {rp(pnl_total)} ({pnl_pct:+.2f}%) |')
L.append(f'| Cash tersisa | {rp(cash)} |')
L.append(f'| Nilai posisi terbuka (5 koin) | {rp(open_val)} |')
L.append(f'| Jumlah transaksi (entry+exit) | {n_trades} |')
L.append(f'| Jumlah exit (SELL) | {n_sells} |')
L.append(f'| Exit untung / rugi | {wins} / {losses} |')
L.append(f'| Win rate | {win_rate:.1f}% |')
L.append(f'| Rata-rata profit per exit menang | {rp(avg_win)} |')
L.append(f'| Rata-rata rugi per exit kalah | {rp(avg_loss)} |')
L.append(f'| PnL realisasi (semua SELL) | {rp(total_realized)} |')
L.append(f'| PnL unrealized posisi terbuka | {rp(eq_final - cash - open_val)} (mark-to-market) |')
L.append('')
L.append('> Fee diasumsikan 0,3% per sisi (sudah termasuk dalam net_idr di data).')
L.append('')

# ---- tabel per koin ----
L.append('## Rekap per Koin (trade realisasi)')
L.append('')
L.append('| Koin | Jml Beli | Jml Jual | Total Beli (IDR) | Total Jual Bersih (IDR) | PnL Realisasi (IDR) | Menang/Kalah |')
L.append('|---|---|---|---|---|---|---|')
rows = []
for sym, c in sorted(per_coin.items(), key=lambda x: x[1]['pnl'], reverse=True):
    rows.append(f'| {sym.replace("/IDR","")} | {c["n_buy"]} | {c["n_sell"]} | {rp(c["buy"])} | {rp(c["sell"])} | {rp(c["pnl"])} | {c["wins"]}/{c["losses"]} |')
L.extend(rows)
L.append('')
L.append('**Posisi terbuka (belum direalisasi) per 05:00 WIB:**')
L.append('')
L.append('| Koin | Entry (IDR) | Investasi (IDR) | High Water (IDR) | Harga Sekarang (IDR) | Unrealized % |')
L.append('|---|---|---|---|---|---|')
for p in positions:
    sym = p['symbol']
    px = prices.get(sym)
    upct = f'{unreal[sym]:+.2f}%' if unreal.get(sym) is not None else 'n/a'
    px_txt = f'{px:,.0f}'.replace(',', '.') if px else 'n/a'
    L.append(f'| {sym.replace("/IDR","")} | {p["entry_price"]:,.0f}'.replace(',', '.') + f' | {rp(p["invested"])} | {p["high_water"]:,.0f}'.replace(',', '.') + f' | {px_txt} | {upct} |')
L.append('')

# ---- semua transaksi kronologis ----
L.append('## Daftar Semua Transaksi (kronologis)')
L.append('')
L.append('| # | Waktu (WIB) | Sisi | Koin | Harga (IDR) | Jumlah (IDR) | PnL Realisasi (IDR) | Alasan |')
L.append('|---|---|---|---|---|---|---|---|')
for i, t in enumerate(trades, 1):
    sym = t['symbol'].replace('/IDR', '')
    pnl = t.get('realized_pnl_idr')
    pnl_txt = f'{rp(pnl)}' if pnl is not None else '-'
    amt = t.get('net_idr') if t['side'] == 'SELL' else t.get('idr')
    L.append(f'| {i} | {wib(t["ts"])} | {t["side"]} | {sym} | {t["price"]:,.0f}'.replace(',', '.') + f' | {rp(amt)} | {pnl_txt} | {t.get("reason", "-")} |')
L.append('')

# ---- equity ticks (sampel per ~3 tick biar ringkas) ----
L.append('## Perjalanan Equity (setiap ±30 menit)')
L.append('')
L.append('| Waktu (WIB) | Equity (IDR) | PnL % | Jumlah Posisi |')
L.append('|---|---|---|---|')
step = max(1, len(ticks) // 20)
for t in ticks[::step]:
    L.append(f'| {wib(t["ts"])} | {rp(t["equity"])} | {t["pnl_pct"]:+.2f}% | {t["n_pos"]} |')
if len(ticks) % step != 0:
    t = ticks[-1]
    L.append(f'| {wib(t["ts"])} | {rp(t["equity"])} | {t["pnl_pct"]:+.2f}% | {t["n_pos"]} |')
L.append('')

# ---- analisis ----
L.append('## Analisis & Kesimpulan')
L.append('')
L.append(f'- Strategi menjalankan **{n_trades} transaksi** dalam 5,5 jam: rotasi agresif mengikuti skor tertinggi top-30 (max 5 posisi, 1 entry/tick).')
L.append(f'- **Win rate {win_rate:.0f}%** dengan rata-rata menang **{rp(avg_win)}** vs rata-rata kalah **{rp(avg_loss)}**. Rasio risk/reward nyata ~1:1 setelah fee, tidak cukup untuk menutup win rate di bawah 55%.')
L.append('- Pola khas terlihat: beberapa posisi sempat **+3 s/d +14%** lalu trailing stop 4% memotong profit (SKR +14,32% adalah pengecualian; UAI +3,8% malah berakhir -3,78% setelah trailing dari high).')
L.append('- **Cut-loss -5% sering terlewat** karena tick tiap ±9 menit: ZORA -7,41%, POND -7,31%, SKR -7,16% — harga sudah jatuh lebih dalam sebelum sempat dieksekusi.')
L.append(f'- Hasil akhir **{pnl_pct:+.2f}%** ({rp(pnl_total)}) — konsisten dengan temuan simulasi-simulasi sebelumnya (expectancy negatif). Dengan fee 0,6% per round-trip dan volatilitas tinggi koin kecil, trading aktif frekuensi tinggi **tidak layak** untuk modal kecil.')
L.append('- Posisi yang masih terbuka (ETH, SKYAI, BICO, XRP, BTC) dipertahankan bot karena belum menyentuh TP/trailing/SL — ini hanya nilai buku, bukan realisasi.')
L.append('')
L.append('---')
L.append(f'*Dibuat otomatis dari `strategy_state.json` — {datetime.now().strftime("%d/%m/%Y %H:%M")} WIB.*')
L.append('')

out_path = os.path.join(BASE, 'laporan_simulasi_indodax_20260901.md')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))

print('OK ditulis ke:', out_path)
print('Total baris markdown:', len(L))
print('Equity final:', rp(eq_final), '| PnL:', rp(pnl_total), f'({pnl_pct:+.2f}%)')
print('Per koin (realisasi):')
for sym, c in sorted(per_coin.items(), key=lambda x: x[1]['pnl'], reverse=True):
    print('  ', sym.replace('/IDR', ''), 'pnl', rp(c['pnl']), f'({c["wins"]}W/{c["losses"]}L)')
