"""
Indodax Trader Bot — integrasi screener Top-30 + adaptive strategy engine.

Alur tiap siklus (mode PAPER, tanpa order nyata):
  1. SCREEN: ambil Top 30 volume IDR 24h → hitung skor teknikal multi-indikator
  2. FILTER: buang stablecoin (USDT/USDC), pilih kandidat skor>=65
  3. BACKTEST: untuk tiap kandidat, replay 500 candle tf 1h → estimasi pnl net
  4. PILIH: coin dgn skor tinggi & backtest paling mendekati/melebihi target >=2% net
  5. ENTRY virtual (kalau belum ada posisi & ada sinyal) / EXIT (kalau TP/SL/regime berubah)
  6. LAPORAN ringkas

CLI:
  python trader_bot.py run --idr 500000 --top 30 --tf 1h
  python trader_bot.py status
  python trader_bot.py reset
  python trader_bot.py cron-add --every 2h     # daftarkan cron job hermes
"""
import ccxt, pandas as pd, pandas_ta as ta, json, os, argparse, sys, subprocess
from datetime import datetime, timezone
from adaptive_bot import fetch_df, eval_signal, execute, new_state, regime, load_state, save_state
import screener

FEE = 0.003
NET_TARGET = 0.02
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trader_state.json')
STABLECOINS = {'USDT', 'USDC'}
MIN_SCORE = 65


def load_state():
    return _ab_load(STATE_FILE)


def save_state(st):
    _ab_save(st, STATE_FILE)


def _ab_load(path):
    from adaptive_bot import load_state as ls
    return ls(path)


def _ab_save(st, path):
    from adaptive_bot import save_state as ss
    ss(st, path)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def screen_top(ex, top_n, tf, limit):
    """Kembalikan list coin Top N volume + skor, diurutkan skor desc."""
    symbols = screener.get_markets(ex)
    tickers = ex.fetch_tickers(symbols)
    rows = []
    for s in symbols:
        t = tickers.get(s)
        if not t:
            continue
        vol = t.get('quoteVolume') or 0
        base = s.split('/')[0]
        if base in STABLECOINS:
            continue
        rows.append((s, vol))
    rows.sort(key=lambda x: -x[1])
    top = rows[:top_n]
    results = []
    for sym, vol in top:
        try:
            df = screener.fetch_df(ex, sym, tf, limit)
            sc = screener.score(df)
            rec, why = screener.recommend(sym, sc)
            results.append({'symbol': sym, 'vol24h_idr': round(vol, 0), **sc, 'rec': rec, 'why': why})
        except Exception as e:
            results.append({'symbol': sym, 'error': str(e)[:80]})
    results.sort(key=lambda x: -(x.get('score') or 0))
    return results


def pick_candidate(screened, ex, tf='1h', limit=500):
    """Dari hasil screening, pilih kandidat terbaik utk entry: skor tinggi + backtest ok + tidak overbought ekstrem."""
    cands = [r for r in screened if 'error' not in r and r.get('score', 0) >= MIN_SCORE]
    if not cands:
        return None, 'Tidak ada kandidat skor >= ' + str(MIN_SCORE)
    reasons = []
    for c in cands[:10]:  # evaluasi 10 teratas saja (hemat waktu)
        try:
            bt = backtest_quick(c['symbol'], tf, limit, 500000)
            c['bt_pnl'] = bt['pnl_pct']
            c['bt_reach2'] = bt['reached_2pct']
            reasons.append({'symbol': c['symbol'], 'score': c['score'], 'bt_pnl': bt['pnl_pct'],
                            'bt_reach2': bt['reached_2pct'], 'regime': c['regime'], 'rsi': c['rsi']})
            # skor + bonus jika backtest >=2%, penalti jika overbought ekstrem (rsi>80)
            adj = c['score']
            if bt['reached_2pct']:
                adj += 8
            if c['rsi'] > 80:
                adj -= 10
            c['_adj'] = adj
        except Exception:
            c['_adj'] = c['score']
            c['bt_pnl'] = -99
            c['bt_reach2'] = False
    # PRIORITAS: coin yang backtest-nya mencapai >=2% & tidak overbought, lalu skor tinggi.
    # Coin tanpa backtest >=2% hanya dipilih bila TIDAK ADA yang lolos.
    eligible = [c for c in cands if c.get('bt_reach2') and c.get('rsi', 99) < 80]
    pool = eligible if eligible else [c for c in cands if c.get('rsi', 99) < 80]
    pool = pool if pool else cands
    pool.sort(key=lambda x: -(x.get('_adj') or 0))
    best = pool[0]
    return best, reasons


def backtest_quick(symbol, tf, limit, idr):
    from adaptive_bot import backtest
    return backtest(symbol, tf, limit, idr)


def run_cycle(idr, top_n, tf, limit=150):
    st = load_state()
    if not st:
        st = new_state(idr)
        save_state(st)
    ex = ccxt.indodax({'enableRateLimit': True})
    screened = screen_top(ex, top_n, tf, limit)
    candidate, reasons = pick_candidate(screened, ex, tf, 500)
    report = {'ts': now_iso(), 'cash': round(st['cash'], 2),
              'position': st['position'], 'candidate': candidate and {k: candidate[k] for k in
              ['symbol', 'score', 'regime', 'rsi', 'rec', 'why']},
              'reasons': reasons, 'top': screened[:15]}
    # --- ENTRY bila tidak ada posisi & ada kandidat layak ---
    if st['position'] is None and candidate:
        sym = candidate['symbol']
        df = fetch_df(sym, tf, 500)
        force = candidate.get('_adj', 0) >= 73 or (candidate.get('bt_reach2') and candidate.get('rsi', 99) < 80)
        sig = eval_signal(df, len(df) - 1, st, sig_ctx={'force_entry': force})
        if sig and sig['action'] == 'BUY':
            execute(st, sig, float(df.iloc[-1]['close']), symbol=sym, state_file=STATE_FILE)
            report['entry'] = {'symbol': sym, 'reason': sig['reason'], 'regime': sig['regime'],
                               'price': float(df.iloc[-1]['close']), 'force': force}
    # --- EXIT bila posisi terbuka & sinyal jual ---
    elif st['position'] is not None:
        sym = st['position'].get('symbol')
        if sym:
            df = fetch_df(sym, tf, 500)
            sig = eval_signal(df, len(df) - 1, st)
            if sig and sig['action'] == 'SELL':
                execute(st, sig, float(df.iloc[-1]['close']), state_file=STATE_FILE)
                report['exit'] = {'symbol': sym, 'reason': sig['reason'],
                                  'price': float(df.iloc[-1]['close'])}
    # --- laporan nilai total ---
    last_total = st['cash']
    if st['position']:
        try:
            px = ccxt.indodax({'enableRateLimit': True}).fetch_ticker(st['position'].get('symbol') or 'BTC/IDR')['last']
            last_total += st['position']['coin'] * px
        except Exception:
            pass
    report['total_value'] = round(last_total, 2)
    report['pnl_pct'] = round((last_total / st['start_cash'] - 1) * 100, 2)
    save_state(st)
    return report


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--idr', type=float, default=500000)
    r.add_argument('--top', type=int, default=30)
    r.add_argument('--tf', default='1h')
    r.add_argument('--limit', type=int, default=150)
    sub.add_parser('status')
    sub.add_parser('reset')
    c = sub.add_parser('cron-add')
    c.add_argument('--every', default='2h', help='interval cron, mis: 2h, 30m, 1d')
    a = ap.parse_args()

    if a.cmd == 'run':
        rep = run_cycle(a.idr, a.top, a.tf, a.limit)
        print(json.dumps(rep, indent=2, default=str))
    elif a.cmd == 'status':
        st = load_state()
        print(json.dumps(st or {'status': 'belum ada state — jalankan: python trader_bot.py run'}, indent=2, default=str))
    elif a.cmd == 'reset':
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(json.dumps({'status': 'reset ok'}))
        else:
            print(json.dumps({'status': 'no state'}))
    elif a.cmd == 'cron-add':
        # Daftarkan cron job hermes (dari CLI session). Di Hermes, cron dibuat via tool; di sini hanya info.
        print(json.dumps({'status': 'info',
                          'cara': 'Gunakan Hermes cronjob tool dengan schedule=' + a.every +
                                  ' dan prompt menjalankan: cd ~/Development/indodax-bot && python trader_bot.py run --idr 500000'}))


if __name__ == '__main__':
    main()
