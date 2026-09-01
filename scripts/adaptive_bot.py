"""
Adaptive Indodax Paper Bot — trend-aware, minimal target +2% NET per cycle.

Regime detection per coin (ADX 14):
  - ADX >= 25  → TRENDING   → strategi trend-following (golden cross masuk,
                              death cross / trailing stop 6% keluar)
  - ADX <  25  → RANGING    → strategi mean-reversion RSI (beli RSI<30,
                              jual RSI>65 ATAU take-profit 2.6% gross ≈ 2% net setelah fee 0.6%)

Backtest mode (data asli Indodax):
  python adaptive_bot.py backtest --symbol BTC/IDR --tf 1h --limit 1000 --idr 500000

Live paper mode (posisi jalan, update berkala):
  python adaptive_bot.py status
  python adaptive_bot.py tick              # evaluasi sinyal & eksekusi virtual
  python adaptive_bot.py reset

Deps: ccxt, pandas, pandas_ta
"""
import ccxt, pandas as pd, pandas_ta as ta, json, os, argparse
from datetime import datetime, timezone

FEE = 0.003                       # 0.3% per sisi; round-trip 0.6%
NET_TARGET = 0.02                 # target minimal 2% BERSIH per siklus
GROSS_TP = NET_TARGET + 2 * FEE   # ~2.6% gross agar net >= 2%
TRAIL = 0.06                      # trailing stop 6% (mode trending)
ADX_TH = 25                       # ADX >= 25 = trending
RSI_BUY, RSI_SELL = 30, 65
INVEST = 0.95

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'adaptive_state.json')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fetch_df(symbol, tf='1h', limit=500):
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=20, append=True, col_names=('EMA_20',))
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    df.ta.adx(length=14, append=True)
    return df


def load_state(state_file=None):
    path = state_file or STATE_FILE
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_state(st, state_file=None):
    path = state_file or STATE_FILE
    with open(path, 'w') as f:
        json.dump(st, f, indent=2)


def new_state(start_idr):
    return {'mode': 'PAPER-ADAPTIVE', 'created': now_iso(), 'cash': float(start_idr),
            'start_cash': float(start_idr), 'position': None, 'trades': []}


def regime(row):
    """'trending' bila ADX>=threshold dan arah jelas, else 'ranging'."""
    adx = float(row['ADX_14']) if pd.notna(row['ADX_14']) else 0
    return 'trending' if adx >= ADX_TH else 'ranging'


def eval_signal(df, i, st, sig_ctx=None):
    """Evaluasi sinyal pada candle ke-i; return None atau dict aksi. sig_ctx: {force_entry: bool}."""
    r = df.iloc[i]
    close = float(r['close'])
    rsi = float(r['RSI_14']) if pd.notna(r['RSI_14']) else None
    e20 = float(r['EMA_20']) if pd.notna(r['EMA_20']) else None
    e50 = float(r['EMA_50']) if pd.notna(r['EMA_50']) else None
    rg = regime(r)
    pos = st['position']
    ts = r['ts'].isoformat() if hasattr(r['ts'], 'isoformat') else str(r['ts'])

    # ---------- EXIT ----------
    if pos is not None:
        entry = pos['entry_price']
        hi = pos.get('high', entry)
        pnl_net = (close / entry - 1) - 2 * FEE  # estimasi net (asumsi full exit)
        # Mode trending: death cross atau trailing stop
        if pos['regime'] == 'trending':
            if e20 is not None and e50 is not None and e20 < e50:
                return {'action': 'SELL', 'reason': 'death_cross', 'price': close}
            if close <= hi * (1 - TRAIL):
                return {'action': 'SELL', 'reason': 'trailing_stop', 'price': close}
        # Mode ranging: RSI overbought ATAU TP net >= 2%
        if pos['regime'] == 'ranging':
            if rsi is not None and rsi >= RSI_SELL:
                return {'action': 'SELL', 'reason': 'rsi_overbought', 'price': close}
            if pnl_net >= NET_TARGET:
                return {'action': 'SELL', 'reason': 'tp_2pct_net', 'price': close}
        # Safety: hard stop loss 4% (kedua mode)
        if pnl_net <= -0.04:
            return {'action': 'SELL', 'reason': 'stop_loss', 'price': close}
        # update high water mark
        pos['high'] = max(hi, close)
        return None

    # ---------- ENTRY ----------
    if st['cash'] < 10000:
        return None
    # Force entry: dipakai trader_bot saat kandidat skor tinggi & backtest >=2% & tidak overbought ekstrem
    if sig_ctx and sig_ctx.get('force_entry'):
        if rsi is not None and rsi < 80:  # hindari entry saat overbought ekstrem
            return {'action': 'BUY', 'reason': 'screener_top_pick', 'price': close, 'regime': rg}
    if rg == 'trending':
        # golden cross = EMA20 naik menembus EMA50
        if i > 0 and e20 is not None and e50 is not None:
            pe20 = float(df.iloc[i-1]['EMA_20']); pe50 = float(df.iloc[i-1]['EMA_50'])
            if pd.notna(pe20) and pd.notna(pe50) and pe20 <= pe50 and e20 > e50:
                return {'action': 'BUY', 'reason': 'golden_cross', 'price': close, 'regime': rg}
    else:
        # ranging: RSI oversold = peluang reversal
        if rsi is not None and rsi <= RSI_BUY:
            return {'action': 'BUY', 'reason': 'rsi_oversold', 'price': close, 'regime': rg}
    return None


def execute(st, sig, close, symbol=None, state_file=None):
    pos = st['position']
    if sig['action'] == 'BUY':
        inv = st['cash'] * INVEST
        fee = inv * FEE
        coin = (inv - fee) / close
        st['position'] = {'symbol': symbol, 'regime': sig['regime'], 'entry_price': close,
                          'coin': coin, 'high': close, 'invested': inv, 'entry_ts': now_iso(),
                          'entry_reason': sig['reason']}
        st['cash'] -= inv
        st['trades'].append({'ts': now_iso(), 'side': 'BUY', 'price': close, 'coin': coin,
                             'idr': inv, 'fee': fee, 'reason': sig['reason'], 'regime': sig['regime'],
                             'symbol': symbol})
    else:  # SELL
        coin = pos['coin']
        gross = coin * close
        fee = gross * FEE
        net = gross - fee
        st['cash'] += net
        realized = net - pos['invested']
        st['trades'].append({'ts': now_iso(), 'side': 'SELL', 'price': close, 'coin': coin,
                             'idr': net, 'fee': fee, 'reason': sig['reason'],
                             'regime': pos['regime'], 'realized_pnl_idr': round(realized, 2),
                             'realized_pnl_pct': round((realized / pos['invested']) * 100, 2)})
        st['position'] = None
    save_state(st, state_file)


# ---------------- BACKTEST ----------------
def backtest(symbol, tf, limit, start_idr):
    df = fetch_df(symbol, tf, limit)
    st = new_state(start_idr)
    # Simulasikan candle per candle, harga eksekusi = close candle (konservatif)
    for i in range(2, len(df)):
        sig = eval_signal(df, i, st)
        if sig:
            execute(st, sig, float(df.iloc[i]['close']))
    # Mark-to-market
    last = float(df.iloc[-1]['close'])
    final = st['cash'] + (st['position']['coin'] * last if st['position'] else 0)
    buys = [t for t in st['trades'] if t['side'] == 'BUY']
    sells = [t for t in st['trades'] if t['side'] == 'SELL']
    realized = sum(t.get('realized_pnl_idr', 0) for t in sells)
    pnl_pct = (final / start_idr - 1) * 100
    bh = start_idr * (last / float(df.iloc[0]['close']))
    return {
        'symbol': symbol, 'tf': tf, 'candles': len(df),
        'period': f"{df.iloc[0]['ts']} → {df.iloc[-1]['ts']}",
        'start_idr': start_idr, 'final_value_idr': round(final, 2),
        'pnl_pct': round(pnl_pct, 2), 'net_target_pct': NET_TARGET * 100,
        'reached_2pct': final >= start_idr * (1 + NET_TARGET),
        'buyhold_pct': round((bh / start_idr - 1) * 100, 2),
        'beat_buyhold': final > bh,
        'trades': len(st['trades']), 'buys': len(buys), 'sells': len(sells),
        'realized_pnl_idr': round(realized, 2),
        'last_regime': regime(df.iloc[-1]),
        'trade_log': st['trades'][-40:],
        'fee_rate': FEE,
        'note': 'Simulasi PAPER, data OHLCV asli Indodax, eksekusi di close candle (konservatif).',
    }


def main():
    ap = argparse.ArgumentParser(description='Adaptive Indodax paper bot')
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('backtest')
    b.add_argument('--symbol', default='BTC/IDR'); b.add_argument('--tf', default='1h')
    b.add_argument('--limit', type=int, default=1000); b.add_argument('--idr', type=float, default=500000)
    sub.add_parser('status')
    sub.add_parser('reset')
    t = sub.add_parser('tick')
    t.add_argument('--symbol', default='BTC/IDR'); t.add_argument('--tf', default='1h')
    a = ap.parse_args()

    if a.cmd == 'backtest':
        print(json.dumps(backtest(a.symbol, a.tf, a.limit, a.idr), indent=2, default=str))
    elif a.cmd == 'reset':
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE); print(json.dumps({'status': 'reset ok'}))
        else:
            print(json.dumps({'status': 'no state'}))
    elif a.cmd == 'status':
        st = load_state()
        print(json.dumps(st or {'status': 'belum init'}, indent=2, default=str))
    elif a.cmd == 'tick':
        st = load_state()
        if not st:
            print(json.dumps({'status': 'error', 'msg': 'belum ada state — jalankan backtest dulu atau init'}))
        else:
            df = fetch_df(a.symbol, a.tf, 200)
            sig = eval_signal(df, len(df)-1, st)
            r = df.iloc[-1]
            print(json.dumps({'status': 'ok', 'symbol': a.symbol, 'last_price': float(r['close']),
                              'rsi': round(float(r['RSI_14']), 2) if pd.notna(r['RSI_14']) else None,
                              'regime': regime(r), 'signal': sig, 'cash': st['cash'],
                              'position': st['position']}, indent=2, default=str))


if __name__ == '__main__':
    main()
