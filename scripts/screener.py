"""
Indodax Top-30 Volume Screener — multi-indikator teknikal.

Skor per coin (0-100), bobot:
  - Momentum (ROC 10/20/50)          30%
  - StochRSI (K/D)                    20%
  - RSI 14 (posisi)                   15%
  - MA trend (EMA20 vs EMA50 vs price) 15%
  - Volume (relatif vs rata-rata)     10%
  - ADX/regime (trending bonus)       10%

Output: ranking coin, rekomendasi BUY (skor tinggi + alasan), WATCH, atau AVOID.
Data: OHLCV asli Indodax via ccxt (publik, tanpa API key).

  python screener.py --top 30 --tf 1h --limit 150
  python screener.py --top 30 --tf 4h --limit 150 --min-idr 50000000000
"""
import ccxt, pandas as pd, pandas_ta as ta, json, argparse, sys

# Bobot indikator
W = {'mom': 0.30, 'stoch': 0.20, 'rsi': 0.15, 'ma': 0.15, 'vol': 0.10, 'adx': 0.10}


def get_markets(ex):
    ex.load_markets()
    return [s for s in ex.symbols if s.endswith('/IDR')]


def fetch_df(ex, symbol, tf, limit):
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.ta.rsi(length=14, append=True)
    df.ta.stochrsi(length=14, append=True)
    df.ta.ema(length=20, append=True, col_names=('EMA_20',))
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    if len(df) >= 200:
        df.ta.ema(length=200, append=True, col_names=('EMA_200',))
    df.ta.adx(length=14, append=True)
    df.ta.roc(length=10, append=True)
    df.ta.roc(length=20, append=True)
    return df


def score(df):
    """Hitung skor teknikal 0-100 untuk coin terakhir df."""
    r = df.iloc[-1]
    px = float(r['close'])
    mom10 = float(r['ROC_10']) if pd.notna(r['ROC_10']) else 0
    mom20 = float(r['ROC_20']) if pd.notna(r['ROC_20']) else 0
    rsi = float(r['RSI_14']) if pd.notna(r['RSI_14']) else 50
    sk = float(r['STOCHRSIk_14_14_3_3']) if pd.notna(r['STOCHRSIk_14_14_3_3']) else 50
    sd = float(r['STOCHRSId_14_14_3_3']) if pd.notna(r['STOCHRSId_14_14_3_3']) else 50
    e20 = float(r['EMA_20']) if pd.notna(r['EMA_20']) else px
    e50 = float(r['EMA_50']) if pd.notna(r['EMA_50']) else px
    e200 = float(r['EMA_200']) if 'EMA_200' in df.columns and pd.notna(r['EMA_200']) else e50
    adx = float(r['ADX_14']) if pd.notna(r['ADX_14']) else 0
    vol = float(r['volume'])
    vol_ma = df['volume'].tail(20).mean()
    vol_ratio = vol / vol_ma if vol_ma else 1

    # --- sub-skor ---
    # momentum: ROC positif besar = bagus; range -5%..+5%
    s_mom = min(max((mom10 + mom20) / 2 / 5, -1), 1) * 50 + 50
    # stochrsi: K naik di atas D = bullish
    s_stoch = 50 + (sk - sd) * 10 + (sk - 50) * 0.5
    s_stoch = min(max(s_stoch, 0), 100)
    # rsi: 40-65 sweet spot bullish; >80 overbought; <30 oversold (potensi rebound)
    if 45 <= rsi <= 65:
        s_rsi = 80
    elif 40 <= rsi < 45 or 65 < rsi <= 70:
        s_rsi = 60
    elif rsi < 30:
        s_rsi = 55  # oversold, potensi reversal (mode ranging)
    elif rsi > 70:
        s_rsi = 35
    else:
        s_rsi = 45
    # ma trend: price > EMA20 > EMA50 = uptrend kuat
    s_ma = 0
    s_ma += 40 if px > e20 else 10
    s_ma += 30 if e20 > e50 else 10
    s_ma += 30 if px > e50 else 10
    # volume: rasio 1.2+ = minat
    s_vol = min(100, vol_ratio * 60) if vol_ratio >= 1 else max(20, vol_ratio * 60)
    # adx: trending kuat
    s_adx = min(100, adx * 2.5) if adx >= 25 else 40  # ranging tetap punya nilai (reversal)

    total = (W['mom'] * s_mom + W['stoch'] * s_stoch + W['rsi'] * s_rsi +
             W['ma'] * s_ma + W['vol'] * s_vol + W['adx'] * s_adx)
    regime = 'trending' if adx >= 25 else 'ranging'
    return {
        'price': round(px, 2), 'roc10': round(mom10, 2), 'roc20': round(mom20, 2),
        'rsi': round(rsi, 2), 'stoch_k': round(sk, 2), 'stoch_d': round(sd, 2),
        'ema20': round(e20, 2), 'ema50': round(e50, 2), 'ema200': round(e200, 2),
        'adx': round(adx, 2), 'regime': regime, 'vol_ratio': round(vol_ratio, 2),
        'score': round(total, 2), 'sub': {k: round(v, 1) for k, v in
            {'mom': s_mom, 'stoch': s_stoch, 'rsi': s_rsi, 'ma': s_ma, 'vol': s_vol, 'adx': s_adx}.items()}
    }


def recommend(coin, s):
    if s['score'] >= 70 and s['regime'] == 'trending' and s['rsi'] < 70:
        return 'STRONG BUY', 'tren naik kuat, momentum & volume sehat'
    if s['score'] >= 60:
        if s['regime'] == 'trending':
            return 'BUY', 'tren naik, momentum positif'
        return 'WATCH (ranging)', 'pantau reversal / tunggu breakout'
    if s['score'] >= 50:
        return 'HOLD/WATCH', 'netral, belum ada sinyal jelas'
    return 'AVOID', 'lemah / overbought / tren turun'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=30)
    ap.add_argument('--tf', default='1h')
    ap.add_argument('--limit', type=int, default=150)
    ap.add_argument('--min-idr', type=float, default=0)
    a = ap.parse_args()

    ex = ccxt.indodax({'enableRateLimit': True})
    symbols = get_markets(ex)
    # Ambil ticker volume 24h per pair
    tickers = ex.fetch_tickers(symbols)
    rows = []
    for s in symbols:
        t = tickers.get(s)
        if not t: continue
        vol_idr = (t.get('quoteVolume') or 0)
        if vol_idr < a.min_idr: continue
        rows.append((s, vol_idr))
    rows.sort(key=lambda x: -x[1])
    top = rows[:a.top]
    print(f'Top {len(top)} volume (IDR/24h) — timeframe {a.tf}, {a.limit} candle', file=sys.stderr)

    results = []
    for sym, vol in top:
        try:
            df = fetch_df(ex, sym, a.tf, a.limit)
            s = score(df)
            rec, why = recommend(sym, s)
            results.append({'symbol': sym, 'vol24h_idr': round(vol, 0), **s, 'rec': rec, 'why': why})
        except Exception as e:
            results.append({'symbol': sym, 'error': str(e)[:80]})

    results.sort(key=lambda x: -(x.get('score') or 0))
    print(json.dumps(results, indent=2, default=str))


if __name__ == '__main__':
    main()
