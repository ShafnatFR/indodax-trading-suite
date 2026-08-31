"""Analisis kandidat hold 1 bulan — Indodax, timeframe daily.
Hitung: return 30d, RSI14, tren (vs EMA50/200), volatilitas, volume 24h, drawdown 30d.
"""
import ccxt
import pandas as pd
import pandas_ta as ta
import json, statistics

SYMBOLS = ['BTC/IDR', 'ETH/IDR', 'SOL/IDR', 'XRP/IDR', 'DOGE/IDR', 'ADA/IDR',
           'SUI/IDR', 'DOT/IDR', 'LINK/IDR', 'AVAX/IDR']

def load_market():
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    return ex

def analyze(ex, symbol, limit=250):
    bars = ex.fetch_ohlcv(symbol, '1d', limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    last = float(df.iloc[-1]['close'])
    prev30 = float(df.iloc[-30]['close']) if len(df) >= 30 else float(df.iloc[0]['close'])
    ret30 = (last / prev30 - 1) * 100
    # RSI
    df.ta.rsi(length=14, append=True)
    rsi = float(df.iloc[-1]['RSI_14']) if pd.notna(df.iloc[-1]['RSI_14']) else None
    # EMA50/200
    df.ta.ema(length=50, append=True, col_names=('EMA50',))
    df.ta.ema(length=200, append=True, col_names=('EMA200',))
    ema50 = float(df.iloc[-1]['EMA50']) if pd.notna(df.iloc[-1]['EMA50']) else None
    ema200 = float(df.iloc[-1]['EMA200']) if 'EMA200' in df.columns and pd.notna(df.iloc[-1]['EMA200']) else None
    trend = 'UPTREND' if (ema50 and last > ema50) else 'DOWNTREND'
    if ema200 and last > ema200:
        trend = 'UPTREND(200)' if trend == 'UPTREND' else 'DOWNTREND'
    # volatilitas 30d (std harian annualized)
    rets = df['close'].pct_change().dropna().tail(30)
    vol30 = statistics.stdev(rets) * (365 ** 0.5) * 100 if len(rets) > 1 else 0
    # drawdown 30d
    win = df.tail(30)
    hi = float(win['high'].max())
    dd = (last / hi - 1) * 100
    # volume 24h
    vol24 = float(df.iloc[-1]['volume']) * last
    return {'symbol': symbol, 'price': last, 'ret30_pct': round(ret30, 1),
            'rsi14': round(rsi, 1) if rsi else None,
            'ema50': ema50, 'ema200': ema200, 'trend': trend,
            'vol_ann_pct': round(vol30, 0), 'dd30_pct': round(dd, 1),
            'vol24h_idr': round(vol24)}

if __name__ == '__main__':
    ex = load_market()
    out = []
    for s in SYMBOLS:
        try:
            out.append(analyze(ex, s))
            print('ok', s)
        except Exception as e:
            out.append({'symbol': s, 'error': str(e)[:120]})
    print(json.dumps(out, indent=1))
