import importlib.util, os, sys
sys.path.insert(0, '.')
spec = importlib.util.spec_from_file_location('paper_bot', 'paper_bot.py')
pb = importlib.util.module_from_spec(spec); spec.loader.exec_module(pb)
import ccxt, pandas as pd

ex = ccxt.indodax({'enableRateLimit': True}); ex.load_markets()
bars = ex.fetch_ohlcv('BTC/IDR', '1h', limit=100)
t = ex.fetch_ticker('BTC/IDR')

print('=== VERIFIKASI 1: harga candle terakhir vs ticker realtime ===')
last_candle_close = bars[-1][4]
print('close candle 1h terakhir :', last_candle_close)
print('ticker last (realtime)   :', t['last'])
print('ticker bid/ask           :', t.get('bid'), '/', t.get('ask'))
print('selisih                  :', round(abs(last_candle_close - t['last'])/t['last']*100, 4), '%')

df = pd.DataFrame(bars, columns=['ts','o','h','l','c','v'])
df['ts'] = pd.to_datetime(df['ts'], unit='ms')
print('\n=== VERIFIKASI 2: integritas data OHLCV (100 candle) ===')
print('baris:', len(df))
print('high>=low semua:', bool((df['h']>=df['l']).all()))
print('close>0 semua:', bool((df['c']>0).all()))
print('volume>=0 semua:', bool((df['v']>=0).all()))
print('timestamp naik monoton:', bool(df['ts'].is_monotonic_increasing))
diffs = df['ts'].diff().iloc[1:].dt.total_seconds()/3600
print('interval antar candle (jam) unik:', sorted(set(diffs.round(3).tolist()))[:5])