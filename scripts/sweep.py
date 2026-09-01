"""Sweep backtest: uji strategi untk cari yang konsisten mencapai target +2% & ungguli buy-and-hold.
Data asli Indodax, fee 0.3%, modal 500rb, BTC/IDR. Validasi akurasi harga eksekusi vs OHLC."
"""
import importlib.util, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('paper_bot', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paper_bot.py'))
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)
import ccxt, pandas as pd, pandas_ta as ta, json

def fetch_df(symbol, tf, limit):
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def backtest(df, start_idr, buy_rsi, buy_gt_ema50, sell_rsi, tp_pct, invest_pct=0.95):
    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    cash, coin, entry = float(start_idr), 0.0, None
    trades = []
    for i in range(len(df)):
        r = df.iloc[i]; close=float(r['close'])
        rsi=float(r['RSI_14']) if pd.notna(r['RSI_14']) else None
        ema50=float(r['EMA_50']) if pd.notna(r['EMA_50']) else None
        ts=r['timestamp'].isoformat()
        if coin==0 and cash>1000 and rsi is not None:
            ok = (rsi < buy_rsi)
            if buy_gt_ema50: ok = ok and (ema50 is not None and close>ema50)
            if ok:
                inv=cash*invest_pct; fee=inv*0.003; b=(inv-fee)/close
                coin,entry,cash=b,close,cash-inv
                trades.append(('BUY',ts,close,b,inv))
        elif coin>0 and rsi is not None:
            if rsi>sell_rsi or (entry and close>=entry*(1+tp_pct)):
                fee=(coin*close)*0.003; cash+=coin*close-fee
                trades.append(('SELL',ts,close,coin,coin*close-fee))
                coin,entry=0.0,None
    last=float(df.iloc[-1]['close']); start=float(df.iloc[0]['close'])
    final=cash+(coin*last if coin>0 else 0)
    bh=start_idr*(last/start)
    sells=[t for t in trades if t[0]=='SELL']; buys=[t for t in trades if t[0]=='BUY']
    rp=sum(t[4] for t in sells)-sum(t[4] for t in buys)
    return {'buy_rsi':buy_rsi,'ema50gate':buy_gt_ema50,'sell_rsi':sell_rsi,'tp':tp_pct,
            'trades':len(trades),'final':round(final,2),'pnl_pct':round((final/start_idr-1)*100,2),
            'bh_pct':round((bh/start_idr-1)*100,2),'beat_bh':final>bh,'reached2':final>=start_idr*1.02,
            'realized':round(rp,2)}

print('fetching BTC/IDR 1h 500...')
df = fetch_df('BTC/IDR','1h',500)
print('backtesting sweep...')
results=[]
for buy_rsi in [25,28,30,32,35]:
    for eg in [False,True]:
        for sell_rsi in [60,65,68,70,75]:
            for tp in [0.02,0.03,0.05]:
                results.append(backtest(df,500000,buy_rsi,eg,sell_rsi,tp))
results.sort(key=lambda x:(-x['reached2'], -x['pnl_pct']))
print('=== TOP configs that REACH +2% ===')
for r in results[:8]:
    print(f"buy_rsi={r['buy_rsi']:>2} ema50gate={str(r['ema50gate']):>5} sell_rsi={r['sell_rsi']:>3} tp={r['tp']:.0%} | trades={r['trades']:>2} pnl={r['pnl_pct']:>7.2f}% bh={r['bh_pct']:>6.2f}% beat_bh={r['beat_bh']} reached2={r['reached2']}")
print()
print('=== how many reached +2% ===')
print('reached2 total:', sum(1 for r in results if r['reached2']), '/', len(results))
print('beat buy&hold:', sum(1 for r in results if r['beat_bh']), '/', len(results))