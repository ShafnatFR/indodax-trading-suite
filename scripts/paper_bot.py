"""
Indodax Paper Trading Bot — simulasi aman (tanpa order nyata).

- Saldo virtual (IDR + koin), fee disimulasikan (0.2% maker/taker khas Indodax).
- Data harga & OHLCV asli dari Indodax (via ccxt + endpoint publik).
- Analisis teknikal: RSI 14, EMA 20/50, tren, volume.
- Eksekusi buy/sell hanya di buku virtual (state JSON), TIDAK menyentuh exchange.

CLI:
  python paper_bot.py init --idr 10000000                 # mulai saldo virtual
  python paper_bot.py status                              # lihat saldo & posisi
  python paper_bot.py analyze --symbol BTC/IDR --tf 1h    # analisis pasar
  python paper_bot.py buy  --symbol BTC/IDR --idr 500000  # beli senilai IDR (market virtual)
  python paper_bot.py sell --symbol BTC/IDR --coin 0.01   # jual sejumlah koin (market virtual)
  python paper_bot.py report                             # riwayat + P&L
  python paper_bot.py reset                              # reset state

Deps: ccxt, pandas, pandas_ta  (Python 3.12)
"""
import ccxt
import pandas as pd
import pandas_ta as ta
import json
import os
import argparse
import time
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paper_state.json')
FEE_RATE = 0.003  # 0.3% per sisi (fee Indodax yang lebih realistis; round-trip 0.6%)
SUPPORTED = ['BTC/IDR', 'ETH/IDR', 'USDT/IDR', 'SOL/IDR', 'BNB/IDR', 'XRP/IDR', 'ADA/IDR', 'DOGE/IDR']


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return None


def _save_state(st):
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f, indent=2)


def _new_state(start_idr):
    return {
        'mode': 'PAPER',
        'created': _now_iso(),
        'cash_idr': float(start_idr),
        'positions': {},          # symbol -> {coin: float, avg_price: float}
        'trades': [],             # riwayat
        'start_cash': float(start_idr),
    }


def _get_exchange():
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    return ex


def _fetch_ohlcv(symbol, timeframe='1h', limit=120):
    ex = _get_exchange()
    bars = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df


def _fetch_ticker(symbol):
    ex = _get_exchange()
    t = ex.fetch_ticker(symbol)
    return t['last'] or t['close']


def analyze(symbol, timeframe='1h', limit=120):
    df = _fetch_ohlcv(symbol, timeframe, limit=limit)
    df.ta.rsi(length=14, append=True)
    df.ta.stochrsi(length=14, append=True)
    df.ta.ema(length=20, append=True, col_names=('EMA_20',))
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    rsi = float(latest['RSI_14']) if pd.notna(latest['RSI_14']) else None
    ema20 = float(latest['EMA_20']) if pd.notna(latest['EMA_20']) else None
    ema50 = float(latest['EMA_50']) if pd.notna(latest['EMA_50']) else None
    price = float(latest['close'])
    trend = 'Bullish' if (ema50 is not None and price > ema50) else ('Bearish' if ema50 is not None else 'Unknown')
    signal = 'NEUTRAL'
    if rsi is not None:
        if rsi >= 70:
            signal = 'OVERBOUGHT (potensi koreksi)'
        elif rsi <= 30:
            signal = 'OVERSOLD (potensi rebound)'
        elif ema20 is not None and ema50 is not None:
            if ema20 > ema50 and price > ema20:
                signal = 'BULLISH MOMENTUM'
            elif ema20 < ema50 and price < ema20:
                signal = 'BEARISH MOMENTUM'
    return {
        'symbol': symbol, 'timeframe': timeframe,
        'price': round(price, 2),
        'change_pct': round((price / float(prev['close']) - 1) * 100, 2) if prev['close'] else 0,
        'volume_24h': float(latest['volume']),
        'rsi_14': round(rsi, 2) if rsi is not None else None,
        'ema_20': round(ema20, 2) if ema20 is not None else None,
        'ema_50': round(ema50, 2) if ema50 is not None else None,
        'trend': trend,
        'signal': signal,
        'recommendation': (
            'Kandidat BUY (oversold)' if rsi is not None and rsi <= 30 else
            'Kandidat SELL/TP (overbought)' if rsi is not None and rsi >= 70 else
            'Hold / tunggu sinyal'
        ),
    }


def paper_buy(symbol, idr_amount):
    st = _load_state()
    if not st:
        return {'status': 'error', 'message': 'Belum init. Jalankan: python paper_bot.py init'}
    price = _fetch_ticker(symbol)
    idr_amount = float(idr_amount)
    if idr_amount <= 0:
        return {'status': 'error', 'message': 'Nominal harus > 0'}
    fee_idr = idr_amount * FEE_RATE
    total_cost = idr_amount + fee_idr
    if total_cost > st['cash_idr']:
        return {'status': 'error', 'message': f'Saldo IDR kurang: butuh {total_cost:,.0f}, saldo {st["cash_idr"]:,.0f}'}
    coin = (idr_amount - fee_idr) / price if price else 0
    st['cash_idr'] -= total_cost
    pos = st['positions'].get(symbol, {'coin': 0.0, 'avg_price': 0.0})
    new_coin = pos['coin'] + coin
    pos['avg_price'] = ((pos['coin'] * pos['avg_price']) + coin * price) / new_coin if new_coin else 0
    pos['coin'] = new_coin
    st['positions'][symbol] = pos
    st['trades'].append({
        'ts': _now_iso(), 'side': 'BUY', 'symbol': symbol, 'price': round(price, 2),
        'coin': round(coin, 8), 'idr': round(idr_amount, 2), 'fee_idr': round(fee_idr, 2),
    })
    _save_state(st)
    return {'status': 'success', 'action': 'PAPER BUY', 'symbol': symbol, 'price': round(price, 2),
            'coin': round(coin, 8), 'idr_spent': round(total_cost, 2), 'fee': round(fee_idr, 2),
            'cash_left': round(st['cash_idr'], 2)}


def paper_sell(symbol, coin_amount):
    st = _load_state()
    if not st:
        return {'status': 'error', 'message': 'Belum init. Jalankan: python paper_bot.py init'}
    price = _fetch_ticker(symbol)
    coin_amount = float(coin_amount)
    pos = st['positions'].get(symbol, {'coin': 0.0, 'avg_price': 0.0})
    if coin_amount <= 0:
        return {'status': 'error', 'message': 'Jumlah koin harus > 0'}
    if coin_amount > pos['coin']:
        return {'status': 'error', 'message': f'Posisi {symbol} cuma {pos["coin"]:.8f} coin'}
    gross = coin_amount * price
    fee_idr = gross * FEE_RATE
    net = gross - fee_idr
    st['cash_idr'] += net
    pos['coin'] -= coin_amount
    if pos['coin'] < 1e-12:
        del st['positions'][symbol]
    else:
        st['positions'][symbol] = pos
    st['trades'].append({
        'ts': _now_iso(), 'side': 'SELL', 'symbol': symbol, 'price': round(price, 2),
        'coin': round(coin_amount, 8), 'idr': round(net, 2), 'fee_idr': round(fee_idr, 2),
    })
    _save_state(st)
    return {'status': 'success', 'action': 'PAPER SELL', 'symbol': symbol, 'price': round(price, 2),
            'coin': round(coin_amount, 8), 'idr_received': round(net, 2), 'fee': round(fee_idr, 2),
            'cash_now': round(st['cash_idr'], 2)}


def status_report():
    st = _load_state()
    if not st:
        return {'status': 'error', 'message': 'Belum init.'}
    ex = _get_exchange()
    total_idr = st['cash_idr']
    positions_detail = []
    for sym, pos in st['positions'].items():
        try:
            px = ex.fetch_ticker(sym)['last']
            val = pos['coin'] * px
            total_idr += val
            pnl = (px - pos['avg_price']) * pos['coin']
            positions_detail.append({
                'symbol': sym, 'coin': round(pos['coin'], 8),
                'avg_price': round(pos['avg_price'], 2), 'current_price': round(px, 2),
                'value_idr': round(val, 2), 'pnl_idr': round(pnl, 2),
                'pnl_pct': round((px / pos['avg_price'] - 1) * 100, 2) if pos['avg_price'] else 0,
            })
        except Exception as e:
            positions_detail.append({'symbol': sym, 'error': str(e)[:80]})
    start = st['start_cash']
    total_pnl = total_idr - start
    return {
        'status': 'success', 'mode': 'PAPER',
        'cash_idr': round(st['cash_idr'], 2),
        'positions': positions_detail,
        'total_value_idr': round(total_idr, 2),
        'total_pnl_idr': round(total_pnl, 2),
        'total_pnl_pct': round((total_idr / start - 1) * 100, 2) if start else 0,
        'trade_count': len(st['trades']),
        'created': st['created'],
    }


def replay(symbol, timeframe='1h', limit=500, start_idr=500000, strategy='rsi_ema'):
    """
    Simulasi historis (replay) di buku virtual dengan data OHLCV asli Indodax.
    Strategi default: beli saat RSI<32 + harga di atas EMA50 (dip) / oversold; jual saat RSI>68 (overbought) / take profit.
    Eksekusi memakai harga close candle (konservatif — tidak memakai harga ideal).
    """
    df = _fetch_ohlcv(symbol, timeframe, limit=limit)
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=20, append=True, col_names=('EMA_20',))
    df.ta.ema(length=50, append=True, col_names=('EMA_50',))

    cash = float(start_idr)
    coin = 0.0
    avg_price = 0.0
    trades = []
    entry_price = None

    for i in range(len(df)):
        row = df.iloc[i]
        close = float(row['close'])
        rsi = float(row['RSI_14']) if pd.notna(row['RSI_14']) else None
        ema20 = float(row['EMA_20']) if pd.notna(row['EMA_20']) else None
        ema50 = float(row['EMA_50']) if pd.notna(row['EMA_50']) else None
        ts = row['timestamp'].isoformat()

        # --- BUY signal ---
        if coin == 0 and cash > 10000 and rsi is not None and ema50 is not None:
            if rsi < 32 and close > ema50:      # dip in uptrend
                invest = cash * 0.95
                fee = invest * FEE_RATE
                bought = (invest - fee) / close
                coin = bought
                avg_price = close
                cash -= invest
                entry_price = close
                trades.append({'ts': ts, 'side': 'BUY', 'price': close, 'coin': bought, 'idr': invest})
            elif rsi < 28:                       # deep oversold
                invest = cash * 0.95
                fee = invest * FEE_RATE
                bought = (invest - fee) / close
                coin = bought
                avg_price = close
                cash -= invest
                entry_price = close
                trades.append({'ts': ts, 'side': 'BUY', 'price': close, 'coin': bought, 'idr': invest})

        # --- SELL signal ---
        elif coin > 0 and rsi is not None:
            if rsi > 68:
                fee = (coin * close) * FEE_RATE
                cash += coin * close - fee
                trades.append({'ts': ts, 'side': 'SELL', 'price': close, 'coin': coin, 'idr': coin * close - fee})
                coin = 0.0
                avg_price = 0.0
                entry_price = None
            elif entry_price and close >= entry_price * 1.03:   # take profit 3%
                fee = (coin * close) * FEE_RATE
                cash += coin * close - fee
                trades.append({'ts': ts, 'side': 'SELL', 'price': close, 'coin': coin, 'idr': coin * close - fee})
                coin = 0.0
                avg_price = 0.0
                entry_price = None

    # Mark-to-market akhir
    last_price = float(df.iloc[-1]['close'])
    final_value = cash + (coin * last_price if coin > 0 else 0)
    start_price = float(df.iloc[0]['close'])
    buyhold_value = start_idr * (last_price / start_price)

    buys = [t for t in trades if t['side'] == 'BUY']
    sells = [t for t in trades if t['side'] == 'SELL']
    realized_pnl = sum(t['idr'] for t in sells) - sum(t['idr'] for t in buys)

    return {
        'status': 'success',
        'mode': 'PAPER REPLAY',
        'symbol': symbol, 'timeframe': timeframe, 'candles': len(df),
        'period': f"{df.iloc[0]['timestamp'].isoformat()} → {df.iloc[-1]['timestamp'].isoformat()}",
        'start_price': round(start_price, 2), 'end_price': round(last_price, 2),
        'market_move_pct': round((last_price / start_price - 1) * 100, 2),
        'start_idr': round(start_idr, 2),
        'final_value_idr': round(final_value, 2),
        'strategy_pnl_pct': round((final_value / start_idr - 1) * 100, 2),
        'buyhold_value_idr': round(buyhold_value, 2),
        'buyhold_pnl_pct': round((buyhold_value / start_idr - 1) * 100, 2),
        'beat_buyhold': final_value > buyhold_value,
        'trade_count': len(trades),
        'buys': len(buys), 'sells': len(sells),
        'realized_pnl_idr': round(realized_pnl, 2),
        'positions_open': coin > 0,
        'trades': trades[-30:],
        'fee_rate': FEE_RATE,
        'note': 'EKSEKUSI VIRTUAL — tidak ada order nyata. Harga eksekusi = close candle asli Indodax.',
    }


def main():
    parser = argparse.ArgumentParser(description='Indodax Paper Trading Bot')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_init = sub.add_parser('init', help='Mulai saldo virtual')
    p_init.add_argument('--idr', type=float, default=10_000_000, help='Saldo awal IDR (default 10jt)')

    sub.add_parser('status', help='Saldo & posisi')
    sub.add_parser('report', help='Riwayat trade + P&L')
    sub.add_parser('reset', help='Reset state')

    p_a = sub.add_parser('analyze', help='Analisis teknikal')
    p_a.add_argument('--symbol', default='BTC/IDR')
    p_a.add_argument('--tf', default='1h', help='15m, 1h, 4h, 1d')
    p_a.add_argument('--limit', type=int, default=120)

    p_b = sub.add_parser('buy', help='Beli virtual (market)')
    p_b.add_argument('--symbol', default='BTC/IDR')
    p_b.add_argument('--idr', type=float, required=True, help='Nominal IDR')

    p_s = sub.add_parser('sell', help='Jual virtual (market)')
    p_s.add_argument('--symbol', default='BTC/IDR')
    p_s.add_argument('--coin', type=float, required=True, help='Jumlah koin')

    p_r = sub.add_parser('replay', help='Simulasi historis (replay) dengan data asli')
    p_r.add_argument('--symbol', default='BTC/IDR')
    p_r.add_argument('--tf', default='1h', help='15m, 1h, 4h, 1d')
    p_r.add_argument('--limit', type=int, default=500, help='Jumlah candle (default 500)')
    p_r.add_argument('--idr', type=float, default=500000, help='Modal virtual (default 500rb)')

    args = parser.parse_args()

    if args.cmd == 'init':
        st = _new_state(args.idr)
        _save_state(st)
        print(json.dumps({'status': 'success', 'message': f'Paper bot dimulai dengan {args.idr:,.0f} IDR virtual', 'state_file': STATE_FILE}))
    elif args.cmd == 'status':
        print(json.dumps(status_report(), default=str, indent=2))
    elif args.cmd == 'report':
        st = _load_state()
        if not st:
            print(json.dumps({'status': 'error', 'message': 'Belum init.'}))
        else:
            print(json.dumps({'status': 'success', 'trades': st['trades'][-50:], 'start_cash': st['start_cash']}, indent=2))
    elif args.cmd == 'reset':
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(json.dumps({'status': 'success', 'message': 'State direset.'}))
        else:
            print(json.dumps({'status': 'error', 'message': 'Tidak ada state.'}))
    elif args.cmd == 'analyze':
        print(json.dumps(analyze(args.symbol, args.tf, args.limit), indent=2))
    elif args.cmd == 'buy':
        print(json.dumps(paper_buy(args.symbol, args.idr), default=str, indent=2))
    elif args.cmd == 'sell':
        print(json.dumps(paper_sell(args.symbol, args.coin), default=str, indent=2))
    elif args.cmd == 'replay':
        print(json.dumps(replay(args.symbol, args.tf, args.limit, args.idr), default=str, indent=2))


if __name__ == '__main__':
    main()
