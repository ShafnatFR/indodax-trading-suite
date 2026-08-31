"""
Indodax Strategy Bot — per brief: SCREEN -> SCORE -> ENTRY -> EXIT.

1. SCREEN  : top-N volume via /api/summaries (curl; urllib SSL rusak di host ini),
             filter maintenance/suspended/red-flag (siren, stablecoin), exclude_symbols.
2. SCORE   : RSI14 + StochRSI + uptrend (MA20 slope) + volume + posisi range harian.
             Pullback ke MA20 di uptrend = skor tertinggi (trend-following).
             Saat bootstrap (>=20 sampel OHLCV Indodax murni belum ada), MA/Bollinger
             di-null -> hanya sinyal scale-invariant (RSI/Stoch/uptrend) yang dipakai.
             Indodax 'change' selalu 0 -> range harian dihitung dari high/low summaries.
3. ENTRY   : beli skor tertinggi, kecuali parabolic (>25% di atas MA20) atau
             di atas Bollinger upper. Max 1 posisi baru/tick, max 5 posisi.
4. EXIT    : target (5%) tercapai -> HOLD kalau tren masih naik, SELL kalau momentum
             melemah. Belum target -> HOLD. Cut loss 5% / trailing 4% / timeout 5 hari
             tetap jalan (semua dievaluasi tiap tick).

Mode:
  PAPER (default) — aman, tidak beli beneran.
  LIVE — ada scaffold (butuh env INDODAX_LIVE_ENABLED=1 + IDX_KEY/IDX_SECRET).

Config: indodax_bot_config.json (edit langsung).
State : strategy_state.json (cash + posisi + high-water).

CLI:
  python strategy_bot.py tick
  python strategy_bot.py status
  python strategy_bot.py reset
  python strategy_bot.py config
"""
import ccxt
import pandas as pd
import pandas_ta as ta
import json, os, subprocess, argparse, time, sys
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'indodax_bot_config.json')
STATE_FILE = os.path.join(BASE_DIR, 'strategy_state.json')

STABLECOINS = {'usdt', 'usdc', 'dai', 'busd', 'tusd', 'usde'}
RED_FLAG = {'siren', 'maintenance', 'suspended'}

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'


# ------------------------------------------------------------------ util
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(st):
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f, indent=2)


def curl_json(url):
    """curl (bukan urllib — SSL rusak di host ini)."""
    r = subprocess.run(['curl', '-4', '-sS', '-A', UA, url],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'curl fail: {r.stderr[:200]}')
    return json.loads(r.stdout)


def sym_to_key(symbol):
    return symbol.lower().replace('/', '_')   # BTC/IDR -> btc_idr


def key_to_sym(key):
    return key.upper().replace('_', '/')       # btc_idr -> BTC/IDR


def fmt_idr(v):
    return f'Rp{v:,.0f}'


# ------------------------------------------------------------------ data
def get_summaries():
    d = curl_json('https://indodax.com/api/summaries')
    return d.get('tickers', {})


def get_ohlcv(symbol, tf='1h', limit=200):
    """History asli Indodax via ccxt (endpoint REST history 'invalid method')."""
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    return df


# ------------------------------------------------------------------ TA
def compute_ta(df):
    """Hitung indikator; MA/Bollinger di-null kalau sampel < 20 (bootstrap)."""
    n = len(df)
    r = {}
    if n >= 30:
        df.ta.rsi(length=14, append=True)
        df.ta.stochrsi(length=14, append=True)
    if n >= 20:
        df.ta.sma(length=20, append=True, col_names=('MA20',))
        df.ta.sma(length=50, append=True, col_names=('MA50',))
        df.ta.bbands(length=20, std=2, append=True)
    if n >= 20:
        r['ma20'] = float(df.iloc[-1]['MA20']) if pd.notna(df.iloc[-1]['MA20']) else None
        r['ma50'] = float(df.iloc[-1]['MA50']) if 'MA50' in df.columns and pd.notna(df.iloc[-1]['MA50']) else None
        # slope MA20 (5 candle)
        if n >= 25 and pd.notna(df.iloc[-6]['MA20']):
            r['ma20_slope'] = (float(df.iloc[-1]['MA20']) - float(df.iloc[-6]['MA20'])) / float(df.iloc[-6]['MA20']) * 100
        else:
            r['ma20_slope'] = 0.0
        bb = 'BBU_20_2.0'
        r['bb_upper'] = float(df.iloc[-1][bb]) if bb in df.columns and pd.notna(df.iloc[-1][bb]) else None
    else:
        r['ma20'] = r['ma50'] = r['ma20_slope'] = r['bb_upper'] = None
    r['rsi'] = float(df.iloc[-1]['RSI_14']) if n >= 30 and pd.notna(df.iloc[-1]['RSI_14']) else None
    if n >= 30:
        r['stoch_k'] = float(df.iloc[-1]['STOCHRSIk_14_14_3_3']) if pd.notna(df.iloc[-1]['STOCHRSIk_14_14_3_3']) else None
        r['stoch_d'] = float(df.iloc[-1]['STOCHRSId_14_14_3_3']) if pd.notna(df.iloc[-1]['STOCHRSId_14_14_3_3']) else None
    else:
        r['stoch_k'] = r['stoch_d'] = None
    r['last'] = float(df.iloc[-1]['close'])
    r['vol_ratio'] = float(df['volume'].tail(1).iloc[0]) / df['volume'].tail(20).mean() if n >= 20 and df['volume'].tail(20).mean() > 0 else 1.0
    return r


# ------------------------------------------------------------------ SCORE
def score_symbol(ta_r, summ):
    """Skor 0-100. Pullback ke MA20 di uptrend = skor tertinggi (trend-following)."""
    last = ta_r['last']
    s = 0.0
    reasons = []
    # --- uptrend: MA20 slope (scale-invariant fallback: last > MA50 jika MA20 null)
    slope = ta_r.get('ma20_slope')
    if slope is not None:
        if slope > 0:
            s += 25; reasons.append('uptrend_ma20')
        else:
            s += 5; reasons.append('downtrend')
    elif ta_r.get('ma50') and last > ta_r['ma50']:
        s += 20; reasons.append('uptrend_ma50')
    else:
        s += 8; reasons.append('trend_unknown')
    # --- pullback ke MA20 (kunci trend-following)
    ma20 = ta_r.get('ma20')
    if ma20:
        ratio = last / ma20
        if 1.00 <= ratio <= 1.05:
            s += 30; reasons.append('pullback_ma20')
        elif 1.05 < ratio <= 1.10:
            s += 20; reasons.append('near_ma20')
        elif 1.10 < ratio <= 1.20:
            s += 10; reasons.append('extended')
        elif ratio > 1.25:
            s += 0; reasons.append('parabolic')
        elif ratio < 1.00:
            s += 10 if (slope or 0) > 0 else 0; reasons.append('below_ma20')
    else:
        s += 12; reasons.append('no_ma20_bootstrap')
    # --- RSI (scale-invariant)
    rsi = ta_r.get('rsi')
    if rsi is not None:
        if 40 <= rsi <= 60:
            s += 20; reasons.append('rsi_ideal')
        elif 60 < rsi <= 70:
            s += 15; reasons.append('rsi_strong')
        elif rsi > 75:
            s += 3; reasons.append('rsi_overbought')
        elif 30 <= rsi < 40:
            s += 15; reasons.append('rsi_pullback')
        else:
            s += 8; reasons.append('rsi_oversold')
    else:
        s += 10; reasons.append('rsi_bootstrap')
    # --- StochRSI momentum
    k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
    if k is not None and d is not None:
        s += 10 if k > d else 3
        reasons.append('stoch_bull' if k > d else 'stoch_bear')
    else:
        s += 5; reasons.append('stoch_bootstrap')
    # --- volume
    vr = ta_r.get('vol_ratio', 1.0)
    s += 5 if vr >= 1.2 else 1
    reasons.append('vol_hi' if vr >= 1.2 else 'vol_low')
    # --- posisi range harian (high/low summaries; change Indodax selalu 0)
    high = float(summ.get('high') or 0); low = float(summ.get('low') or 0)
    pos_range = (last - low) / (high - low) if high > low else 0.5
    if 0.2 <= pos_range <= 0.6:
        s += 10; reasons.append('range_mid')
    elif pos_range < 0.2:
        s += 6; reasons.append('range_low')
    else:
        s += 3; reasons.append('range_high')
    return round(min(s, 100), 1), reasons, pos_range


# ------------------------------------------------------------------ SCREEN
def screen_top(config):
    """Top-N volume via /api/summaries, filter red-flag/stablecoin/maintenance."""
    tickers = get_summaries()
    excl = {x.lower().replace('/', '_') for x in config.get('exclude_symbols', [])}
    rows = []
    for key, t in tickers.items():
        base = key.split('_')[0]
        if base in STABLECOINS or base in RED_FLAG or key in excl:
            continue
        vol = float(t.get('vol_idr') or 0)
        if vol <= 0:
            continue
        rows.append((key, t, vol))
    rows.sort(key=lambda x: -x[2])
    top = rows[:config.get('top_n_volume', 30)]
    out = []
    for key, t, vol in top:
        out.append({'key': key, 'symbol': key_to_sym(key), 'name': t.get('name'),
                    'last': float(t.get('last') or 0), 'high': float(t.get('high') or 0),
                    'low': float(t.get('low') or 0), 'vol_idr': vol})
    return out


# ------------------------------------------------------------------ ENTRY
def entry_guard(ta_r, config):
    """Return (ok, reason). Blokir parabolic / di atas Bollinger upper."""
    ma20 = ta_r.get('ma20')
    if ma20:
        ratio = ta_r['last'] / ma20
        if ratio > 1 + config.get('parabolic_threshold_pct', 25) / 100:
            return False, f'parabolic ({ratio*100:.0f}% di atas MA20)'
    if config.get('bollinger_upper_avoid', True) and ta_r.get('bb_upper') is not None:
        if ta_r['last'] > ta_r['bb_upper']:
            return False, 'di atas Bollinger upper'
    return True, ''


def position_size(config, equity):
    return equity * config.get('risk_per_trade_pct', 10.0) / 100.0


def open_position(st, symbol, price, invest_idr, fee_rate=0.003):
    coin = invest_idr * (1 - fee_rate) / price
    st['positions'].append({
        'symbol': symbol, 'entry_price': price, 'coin': coin, 'invested': invest_idr,
        'entry_ts': now_iso(), 'high_water': price, 'fee_rate': fee_rate,
    })
    st['cash'] -= invest_idr


# ------------------------------------------------------------------ EXIT
def eval_exit(pos, ta_r, summ, config):
    """Return (action, reason). action: HOLD / SELL."""
    last = ta_r['last']
    entry = pos['entry_price']
    pnl = (last / entry - 1) * 100
    pos['high_water'] = max(pos.get('high_water', entry), last)
    held_days = (datetime.now(timezone.utc) - datetime.fromisoformat(pos['entry_ts'])).total_seconds() / 86400

    # cut loss (hard)
    if pnl <= -config.get('hard_stop_loss_pct', 5.0):
        return 'SELL', f'cut_loss ({pnl:.2f}%)'
    # trailing stop
    tr = config.get('trailing_stop_pct', 4.0)
    if tr > 0 and pos['high_water'] > entry:
        if last <= pos['high_water'] * (1 - tr / 100):
            return 'SELL', f'trailing ({tr:.0f}% dari high {pos["high_water"]:,.0f})'
    # timeout
    if held_days >= config.get('max_hold_days', 5):
        return 'SELL', f'timeout ({held_days:.1f} hari)'
    # target tercapai
    if pnl >= config.get('min_target_pct', 5.0):
        slope = ta_r.get('ma20_slope')
        uptrend = (slope is not None and slope > 0) or (ta_r.get('ma50') and last > ta_r['ma50'])
        k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
        momentum_weak = (k is not None and d is not None and k < d) or last < (ta_r.get('ma20') or last)
        if uptrend and not momentum_weak:
            return 'HOLD', f'target {pnl:.2f}% tercapai, tren masih naik'
        return 'SELL', f'target {pnl:.2f}% + momentum melemah'
    return 'HOLD', f'pnl {pnl:.2f}% (belum target)'


# ------------------------------------------------------------------ TICK
def tick(config=None):
    config = config or load_config()
    st = load_state()
    if not st:
        st = {'mode': config.get('mode', 'PAPER'), 'created': now_iso(),
              'cash': float(config.get('start_cash_idr', 500000)),
              'start_cash': float(config.get('start_cash_idr', 500000)),
              'positions': [], 'trades': [], 'ticks': []}
        save_state(st)

    report = {'ts': now_iso(), 'mode': st['mode']}

    # --- EXIT evaluation utk semua posisi ---
    summaries = None
    exited = []
    for pos in list(st['positions']):
        try:
            df = get_ohlcv(pos['symbol'], '1h', 200)
            tr = compute_ta(df)
            if summaries is None:
                summaries = get_summaries()
            summ = summaries.get(sym_to_key(pos['symbol']), {})
            action, reason = eval_exit(pos, tr, summ, config)
            if action == 'SELL':
                px = tr['last']
                gross = pos['coin'] * px
                fee = gross * pos.get('fee_rate', 0.003)
                net = gross - fee
                st['cash'] += net
                realized = net - pos['invested']
                st['trades'].append({'ts': now_iso(), 'side': 'SELL', 'symbol': pos['symbol'],
                                     'price': px, 'coin': pos['coin'], 'net_idr': net,
                                     'realized_pnl_idr': round(realized, 2),
                                     'realized_pnl_pct': round((realized / pos['invested']) * 100, 2),
                                     'reason': reason, 'hold_days': round((datetime.now(timezone.utc) - datetime.fromisoformat(pos['entry_ts'])).total_seconds()/86400, 2)})
                exited.append({'symbol': pos['symbol'], 'reason': reason, 'price': px,
                               'pnl_pct': round((realized / pos['invested']) * 100, 2)})
                st['positions'].remove(pos)
        except Exception as e:
            report.setdefault('exit_errors', []).append({'symbol': pos['symbol'], 'err': str(e)[:100]})
    report['exited'] = exited

    # --- SCREEN ---
    try:
        top = screen_top(config)
        report['screen_count'] = len(top)
        report['top_n'] = top[:config.get('top_n_volume', 30)]
    except Exception as e:
        report['screen_error'] = str(e)[:200]
        save_state(st)
        return report

    # --- SCORE tiap kandidat (hanya yang belum diposisi) ---
    held = {p['symbol'] for p in st['positions']}
    scored = []
    for item in top:
        if item['symbol'] in held:
            continue
        try:
            df = get_ohlcv(item['symbol'], '1h', 200)
            tr = compute_ta(df)
            s, reasons, pos_range = score_symbol(tr, item)
            ok, guard_reason = entry_guard(tr, config)
            scored.append({'symbol': item['symbol'], 'name': item['name'], 'last': tr['last'],
                           'score': s, 'reasons': reasons, 'range_pos': round(pos_range, 2),
                           'rsi': tr.get('rsi'), 'ma20': tr.get('ma20'), 'ma20_slope': tr.get('ma20_slope'),
                           'bb_upper': tr.get('bb_upper'), 'vol_ratio': tr.get('vol_ratio'),
                           'guard_ok': ok, 'guard_reason': guard_reason, 'vol_idr': item['vol_idr']})
        except Exception as e:
            scored.append({'symbol': item['symbol'], 'error': str(e)[:100]})
    scored.sort(key=lambda x: -(x.get('score') or 0))
    report['scored'] = scored[:20]

    # --- ENTRY: max 1 posisi baru/tick, max 5 posisi ---
    new_entries = []
    if len(st['positions']) < config.get('max_positions', 5):
        for c in scored:
            if 'error' in c or not c.get('guard_ok'):
                continue
            if len(new_entries) >= config.get('max_new_positions_per_tick', 1):
                break
            # jangan entry di atas 1 posisi per symbol (sudah difilter held)
            invest = position_size(config, st['cash'] + sum(p['invested'] for p in st['positions']))
            if invest < 10000:
                break
            open_position(st, c['symbol'], c['last'], invest)
            st['trades'].append({'ts': now_iso(), 'side': 'BUY', 'symbol': c['symbol'],
                                 'price': c['last'], 'idr': invest, 'reason': 'score_top',
                                 'score': c['score']})
            new_entries.append({'symbol': c['symbol'], 'price': c['last'], 'invest': round(invest, 2),
                                'score': c['score']})
    report['entries'] = new_entries

    # --- mark-to-market ---
    equity = st['cash']
    for p in st['positions']:
        try:
            df = get_ohlcv(p['symbol'], '1h', 50)
            equity += p['coin'] * float(df.iloc[-1]['close'])
        except Exception:
            equity += p['invested']
    report['cash'] = round(st['cash'], 2)
    report['positions_open'] = len(st['positions'])
    report['positions'] = st['positions']
    report['equity'] = round(equity, 2)
    report['pnl_pct'] = round((equity / st['start_cash'] - 1) * 100, 2)
    st['ticks'].append({'ts': now_iso(), 'equity': round(equity, 2),
                        'pnl_pct': report['pnl_pct'], 'n_pos': len(st['positions'])})
    save_state(st)
    return report


def status_report():
    st = load_state()
    if not st:
        return {'status': 'no state'}
    equity = st['cash']
    for p in st['positions']:
        try:
            df = get_ohlcv(p['symbol'], '1h', 50)
            equity += p['coin'] * float(df.iloc[-1]['close'])
        except Exception:
            equity += p['invested']
    return {'mode': st['mode'], 'cash': round(st['cash'], 2), 'positions': st['positions'],
            'equity': round(equity, 2), 'pnl_pct': round((equity / st['start_cash'] - 1) * 100, 2),
            'start_cash': st['start_cash'], 'trades': st['trades'][-20:],
            'last_ticks': st.get('ticks', [])[-10:]}


def main():
    ap = argparse.ArgumentParser(description='Indodax Strategy Bot')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('tick')
    sub.add_parser('status')
    sub.add_parser('reset')
    sub.add_parser('config')
    sub.add_parser('screen')
    a = ap.parse_args()

    if a.cmd == 'tick':
        print(json.dumps(tick(), indent=2, default=str))
    elif a.cmd == 'status':
        print(json.dumps(status_report(), indent=2, default=str))
    elif a.cmd == 'reset':
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(json.dumps({'status': 'reset ok'}))
        else:
            print(json.dumps({'status': 'no state'}))
    elif a.cmd == 'config':
        print(json.dumps(load_config(), indent=2))
    elif a.cmd == 'screen':
        # screening saja tanpa eksekusi entry — tampilkan ranking + rekomendasi
        config = load_config()
        top = screen_top(config)
        held = {p['symbol'] for p in (load_state() or {}).get('positions', [])}
        out = []
        for item in top:
            try:
                df = get_ohlcv(item['symbol'], '1h', 200)
                tr = compute_ta(df)
                s, reasons, pos_range = score_symbol(tr, item)
                ok, guard_reason = entry_guard(tr, config)
                out.append({'symbol': item['symbol'], 'name': item['name'],
                            'price': tr['last'], 'vol24h_idr': round(item['vol_idr']),
                            'score': s, 'rsi': tr.get('rsi'), 'stoch_k': tr.get('stoch_k'),
                            'stoch_d': tr.get('stoch_d'), 'ma20': tr.get('ma20'),
                            'ma20_slope': tr.get('ma20_slope'), 'bb_upper': tr.get('bb_upper'),
                            'range_pos': round(pos_range, 2), 'vol_ratio': tr.get('vol_ratio'),
                            'entry_ok': ok, 'guard': guard_reason, 'held': item['symbol'] in held,
                            'reasons': reasons,
                            'recommendation': ('TRADE (top pick)' if (ok and not item['symbol'] in held) else
                                               ('HELD' if item['symbol'] in held else 'SKIP (guard)'))})
            except Exception as e:
                out.append({'symbol': item['symbol'], 'error': str(e)[:100]})
        out.sort(key=lambda x: -(x.get('score') or 0))
        print(json.dumps({'config': {k: config[k] for k in ['min_target_pct', 'top_n_volume', 'mode', 'start_cash_idr']},
                          'results': out}, indent=2, default=str))


if __name__ == '__main__':
    main()
