"""
Simulasi compounding: modal Rp1.000.000, 2% net per trade, 5 trade sukses/hari.

Asumsi dibedakan jujur:
  A) Compounding PER TRADE (5x/hari, profit ditambahkan ke modal tiap trade)
     -> modal tumbuh 1.02^5 per hari
  B) Compounding PER HARI (5 trade masing-masing 2% dari modal awal hari itu,
     profit di-reinvest besoknya)
     -> modal tumbuh 1.10 per hari (5 x 2% flat harian)
  C) Flat: 5 x 2% dari modal AWAL Rp1jt tetap (tanpa reinvest) -> Rp100rb/hari
  D) Realistis: 5 trade/hari, tapi hanya 60% win rate; 2% gain saat win,
     -1.5% loss saat loss (fee 0.3% sudah termasuk) -> ekspektasi per trade
     = 0.6*2% - 0.4*1.5% = +0.6%/trade

Fee 0.3% per sisi SUDAH termasuk dalam angka 2% net (target net strategy bot).
Hari trading: 365 (kripto 24/7, tidak ada libur).
"""
import json

START = 1_000_000
NET_PER_TRADE = 0.02
TRADES_PER_DAY = 5
DAYS = 365


def simulate(label, daily_growth, note):
    rows = []
    m = START
    for d in range(0, DAYS + 1, 30):
        rows.append({'day': d, 'balance': round(m, 0)})
        m *= daily_growth
    # final
    m = START
    for _ in range(DAYS):
        m *= daily_growth
    return {'label': label, 'note': note, 'daily_growth_x': round(daily_growth, 6),
            'final_balance': round(m, 0), 'final_pct': round((m / START - 1) * 100, 0),
            'monthly': rows}


results = []
# A) compound per trade: 5x sehari, tiap trade 2% di-reinvest
results.append(simulate('A. Compound per trade (5x/hari, reinvest tiap trade)',
                        (1 + NET_PER_TRADE) ** TRADES_PER_DAY,
                        'Modal tumbuh 1.02^5 = 1.1041x per hari'))
# B) compound per hari
results.append(simulate('B. Compound per hari (5x2% flat, reinvest harian)',
                        1 + 5 * NET_PER_TRADE,
                        'Modal tumbuh 1.10x per hari'))
# C) flat tanpa reinvest
results.append(simulate('C. Flat (tanpa reinvest)',
                        1 + 0,  # tidak tumbuh; profit Rp100rb/hari ditambahkan terpisah
                        'Profit tetap Rp100.000/hari (5 x 2% x 1jt)'))
# C khusus: flat profit harian ditambahkan
m = START
for _ in range(DAYS):
    m += 5 * NET_PER_TRADE * START
results[-1]['final_balance'] = round(m, 0)
results[-1]['final_pct'] = round((m / START - 1) * 100, 0)
results[-1]['monthly'] = [{'day': d, 'balance': round(START + d * 100_000, 0)} for d in range(0, DAYS + 1, 30)]
results[-1]['note'] = 'Profit flat Rp100.000/hari ditambahkan; modal pokok tetap'

# D) realistis 60% win rate
win_rate = 0.60
exp_per_trade = win_rate * NET_PER_TRADE - (1 - win_rate) * 0.015
m = START
for _ in range(DAYS):
    m *= (1 + exp_per_trade) ** TRADES_PER_DAY
results.append(simulate('D. Realistis 60% win rate (ekspektasi +0.6%/trade)',
                        (1 + exp_per_trade) ** TRADES_PER_DAY,
                        f'0.6*2% - 0.4*1.5% = +0.6%/trade -> x{(1+exp_per_trade)**5:.4f}/hari'))

print(json.dumps(results, indent=2))