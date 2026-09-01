"""
Walk-forward optimization: cari strategi yang MINIMAL +2% secara konsisten.
- Train = 60% data awal, Validation = 40% data terbaru (out-of-sample).
- Strategi lolos hanya jika pnl >= 2% di KEDUA window.
- Cross-check di beberapa dataset (BTC 1h/4h, ETH 1h) untuk hindari overfitting.
Data asli Indodax, fee 0.3%, modal 500rb, harga eksekusi = close candle.
"""
import ccxt, pandas as pd, pandas_ta as ta, itertools, json

FEE = 0.003
START = 500000.0

def fetch_df(symbol, tf, limit):
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    return df

def backtest(df, cfg):
    buy_rsi, sell_rsi, ema_gate, trail, tp = cfg
    cash, coin, entry, hi = START, 0.0, None, 0.0
    trades = 0
    for i in range(len(df)):
        r = df.iloc[i]; close = float(r['close'])
        rsi = float(r['RSI_14']) if pd.notna(r['RSI_14']) else None
        ema50 = float(r['EMA_50']) if pd.notna(r['EMA_50']) else None
        if coin == 0:
            if rsi is None: continue
            ok = rsi < buy_rsi and (not ema_gate or (ema50 is not None and close > ema50))
            if ok and cash > 10000:
                inv = cash * 0.95; fee = inv * FEE; b = (inv - fee) / close
                coin, entry, cash, hi, trades = b, close, cash - inv, close, trades + 1
        else:
            if rsi is None: continue
            hi = max(hi, close)
            sell = False
            if trail > 0 and close <= hi * (1 - trail): sell = True
            elif tp > 0 and entry and close >= entry * (1 + tp): sell = True
            elif rsi > sell_rsi: sell = True
            if sell:
                cash += coin * close - (coin * close) * FEE
                coin, entry, hi = 0.0, None, 0.0
    last = float(df.iloc[-1]['close'])
    final = cash + (coin * last if coin > 0 else 0)
    pnl = (final / START - 1) * 100
    return pnl, trades

def sweep(df):
    space = list(itertools.product(
        [30, 33, 35, 38, 40],     # buy_rsi
        [62, 65, 68, 72, 78],     # sell_rsi
        [False, True],            # ema50 gate
        [0.0, 0.02, 0.04, 0.06],  # trailing stop
        [0.0, 0.03, 0.05],        # take profit
    ))
    results = []
    for cfg in space:
        pnl, trades = backtest(df, cfg)
        results.append((cfg, pnl, trades))
    return results

def split(df, frac=0.6):
    n = len(df)
    return df.iloc[:int(n*frac)].copy(), df.iloc[int(n*frac):].copy()

datasets = {
    'BTC/IDR 1h (500)':  fetch_df('BTC/IDR', '1h', 500),
    'BTC/IDR 1h (1000)': fetch_df('BTC/IDR', '1h', 1000),
    'BTC/IDR 4h (500)':  fetch_df('BTC/IDR', '4h', 500),
    'ETH/IDR 1h (500)':  fetch_df('ETH/IDR', '1h', 500),
}

print('=== Walk-forward per dataset: konfigurasi yang lolos train>=2% DAN valid>=2% ===')
passes = {}
for name, df in datasets.items():
    tr, va = split(df)
    res = sweep(tr)
    ok = [(c, p, t) for c, p, t in res if p >= 2.0]
    # validate survivors out-of-sample
    surv = []
    for c, p_tr, t_tr in ok:
        p_va, t_va = backtest(va, c)
        if p_va >= 2.0:
            surv.append((c, round(p_tr,2), round(p_va,2), t_tr, t_va))
    passes[name] = surv
    print(f'\n[{name}] data {len(df)} candle | lolos train: {len(ok)} | lolos train+valid: {len(surv)}')
    for c, p1, p2, t1, t2 in surv[:10]:
        print(f'   buy_rsi={c[0]} sell_rsi={c[1]} emagate={c[2]} trail={c[3]:.0%} tp={c[4]:.0%} | train={p1}% valid={p2}% trades={t1}/{t2}')

# Konfigurasi yang lolos di SEMUA dataset (paling robust)
allnames = list(datasets.keys())
common = None
for name in allnames:
    s = {c for c,_,_,_,_ in passes[name]}
    common = s if common is None else (common & s)
print('\n=== Konfigurasi yang lolos di SEMUA dataset (paling robust) ===')
for c in sorted(common):
    print(f'   buy_rsi={c[0]} sell_rsi={c[1]} emagate={c[2]} trail={c[3]:.0%} tp={c[4]:.0%}')
if not common:
    print('   (tidak ada — cek best per-dataset di atas)')