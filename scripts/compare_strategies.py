"""
Bandingkan 3 pendekatan di data asli, fee 0.3%, modal 500rb:
 A) Buy-and-hold
 B) RSI diskrit (best config BTC 4h: buy_rsi=33, sell_rsi=78, tp=3%)
 C) Trend-following: EMA crossover (20/50) + trailing stop 5%, tanpa take-profit dini
Laporan jujur: mana yang paling konsisten >=2% dan mana yang menang.
"""
import ccxt, pandas as pd, pandas_ta as ta
FEE=0.003; START=500000.0

def fetch(sym,tf,n):
    ex=ccxt.indodax({'enableRateLimit':True}); ex.load_markets()
    b=ex.fetch_ohlcv(sym,tf,limit=n)
    df=pd.DataFrame(b,columns=['ts','o','h','l','c','v']); df['ts']=pd.to_datetime(df['ts'],unit='ms')
    df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'},inplace=True)
    df.ta.rsi(length=14,append=True)
    df.ta.ema(length=20,append=True,col_names=('EMA_20',))
    df.ta.ema(length=50,append=True,col_names=('EMA_50',))
    return df

def ret_pct(final): return (final/START-1)*100

def buyhold(df):
    return START*(float(df.iloc[-1]['close'])/float(df.iloc[0]['close']))

def rsi_diskrit(df):
    cash,coin,entry=START,0.0,None
    for i in range(len(df)):
        r=df.iloc[i]; cl=float(r['close'])
        rsi=float(r['RSI_14']) if pd.notna(r['RSI_14']) else None
        if coin==0 and rsi is not None and rsi<33 and cash>10000:
            inv=cash*0.95; fee=inv*FEE; b=(inv-fee)/cl; coin,entry,cash=b,cl,cash-inv
        elif coin>0 and rsi is not None:
            if rsi>78 or (entry and cl>=entry*1.03):
                cash+=coin*cl-(coin*cl)*FEE; coin,entry=0.0,None
    last=float(df.iloc[-1]['close']); return cash+(coin*last if coin>0 else 0)

def trend_follow(df):
    cash,coin,entry,hi=START,0.0,None,0.0
    for i in range(len(df)):
        r=df.iloc[i]; cl=float(r['close'])
        e20=float(r['EMA_20']) if pd.notna(r['EMA_20']) else None
        e50=float(r['EMA_50']) if pd.notna(r['EMA_50']) else None
        if coin==0 and e20 is not None and e50 is not None:
            prev_e20=float(df.iloc[i-1]['EMA_20']); prev_e50=float(df.iloc[i-1]['EMA_50'])
            if prev_e20<=prev_e50 and e20>e50:      # golden cross
                inv=cash*0.95; fee=inv*FEE; b=(inv-fee)/cl; coin,entry,cash,hi=b,cl,cash-inv,cl
        elif coin>0:
            hi=max(hi,cl)
            if e20 is not None and e50 is not None and e20<e50:   # death cross
                cash+=coin*cl-(coin*cl)*FEE; coin=0.0; entry=None; hi=0.0
            elif cl<=hi*0.95:                                     # trailing stop 5%
                cash+=coin*cl-(coin*cl)*FEE; coin=0.0; entry=None; hi=0.0
    last=float(df.iloc[-1]['close']); return cash+(coin*last if coin>0 else 0)

sets=[fetch('BTC/IDR','1h',500), fetch('BTC/IDR','4h',500), fetch('ETH/IDR','1h',500), fetch('SOL/IDR','1h',500)]
names=['BTC 1h','BTC 4h','ETH 1h','SOL 1h']

print(f"{'dataset':<8} | {'buy-hold':>9} | {'RSI-diskrit':>12} | {'trend-follow':>13}")
for name,df in zip(names,sets):
    bh=ret_pct(buyhold(df)); rs=ret_pct(rsi_diskrit(df)); tf=ret_pct(trend_follow(df))
    print(f"{name:<8} | {bh:>8.2f}% | {rs:>11.2f}% | {tf:>12.2f}%")

print('\nLegenda: angka = total return dari modal Rp500.000, fee 0.3%.')
print('Semua >= 0 = profit. Konversi ke rupiah: profit% x Rp500.000.')