"""Live tick bot Indodax — cron tiap 2 jam sampai 05:00 WIB.
Alur per tick:
  1. Reconcile pending buy/sell (cek status order, konfirmasi fill lewat saldo).
  2. EXIT : posisi aktif dijual kalau pnl >= +2% (take profit) atau <= -5% (stop loss).
  3. ENTRY: kalau posisi kosong & cash >= 10rb -> screen top-30, score, beli limit kandidat terbaik.
Deterministik, 1 posisi, limit order, tanpa leverage. Semua state di live_state.json.
"""
import os, sys, json, time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.expanduser('~'))
import ccxt
import strategy_bot as sb
from idx_v2_client import signed_request

STATE_FILE = os.path.join(BASE_DIR, 'live_state.json')
TARGET_PCT = 2.0
STOP_PCT = 5.0
FEE_RATE = 0.003
MIN_ORDER = 10000.0
START_EQUITY = 49250.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {'positions': [], 'pending_buys': [], 'pending_sells': [],
            'trades': [], 'start_equity': START_EQUITY}


def seed_from_balances(ex):
    """Kalau state kosong tapi saldo exchange punya aset (posisi dibeli manual),
    jadikan posisi aktif. Entry price & invested diambil dari posisi yang sudah
    tercatat di strategy_state.json (bot PAPER) kalau ada — supaya basis PnL benar;
    fallback: harga pasar saat ini."""
    st = load_state()
    if st['positions'] or st['pending_buys'] or st['pending_sells']:
        return st
    # cari posisi yang relevan di state bot paper (entry 475 utk SKR)
    paper_known = {}
    try:
        pst = json.load(open(os.path.join(BASE_DIR, 'strategy_state.json')))
        for p in pst.get('positions', []):
            paper_known[p['symbol']] = (float(p['entry_price']), float(p['invested']))
    except Exception:
        pass
    free, total = get_balances()
    seeded = False
    for asset, qty in total.items():
        if asset in ('IDR', 'BTC') or qty <= 0:
            # BTC = hold manual user (1 bulan), JANGAN pernah dijadikan posisi bot
            continue
        sym = asset + '/IDR'
        if sym in paper_known:
            entry_px, invested = paper_known[sym]
        elif asset == 'SKR' and qty == 101.0:
            # posisi SKR dibeli manual @475 — entry aktual, bukan harga pasar
            entry_px = 475.0
            invested = 47975.0
        else:
            try:
                t = ex.fetch_ticker(sym)
                entry_px = float(t['last'])
            except Exception:
                entry_px = 0.0
            invested = qty * entry_px
        if entry_px <= 0:
            continue
        st['positions'].append({'symbol': sym, 'entry_price': entry_px,
                                'coin': qty, 'invested': invested,
                                'entry_ts': now_iso()})
        st['trades'].append({'ts': now_iso(), 'side': 'BUY', 'symbol': sym,
                             'price': entry_px, 'coin': qty, 'idr': round(invested, 2),
                             'reason': 'seed dari saldo (posisi manual @%s)' % entry_px})
        seeded = True
    if seeded:
        st['start_equity'] = st.get('start_equity', START_EQUITY)
        save_state(st)
    return st


def save_state(st):
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f, indent=2)


def get_balances():
    """Return (free, total) dict per aset."""
    res = signed_request('GET', '/api/v2/account', {'omitZeroBalances': True})
    free, total = {}, {}
    for b in res.get('balances', []):
        f = float(b.get('free', 0) or 0)
        l = float(b.get('locked', 0) or 0)
        free[b['asset']] = f
        total[b['asset']] = f + l
    return free, total


def place_order(symbol_ns, side, qty, price):
    return signed_request('POST', '/api/v2/order', {
        'symbol': symbol_ns, 'side': side, 'type': 'LIMIT',
        'price': str(price), 'quantity': str(qty)})


def cancel_order(symbol_ns, order_id):
    return signed_request('DELETE', '/api/v2/order',
                          {'symbol': symbol_ns, 'orderId': str(order_id)})


def open_order_status(symbol_ns, order_id):
    res = signed_request('GET', '/api/v2/openOrders', {'symbol': symbol_ns})
    if not isinstance(res, list):
        return 'unknown'
    for o in res:
        if str(o.get('orderId')) == str(order_id):
            return o.get('status', 'NEW')
    return 'filled_or_canceled'


def tick():
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    st = load_state()
    free, total = get_balances()
    report = {'ts': now_iso(), 'balances': free}

    # ---- 0) seed posisi dari saldo kalau state kosong ----
    st = seed_from_balances(ex)

    # ---- 1) reconcile pending buys ----
    for pb in list(st.get('pending_buys', [])):
        ns = pb['symbol'].replace('/', '')
        base = pb['symbol'].split('/')[0]
        status = open_order_status(ns, pb['orderId'])
        if status == 'filled_or_canceled':
            if total.get(base, 0.0) >= pb['qty'] * 0.999:
                st['positions'].append({'symbol': pb['symbol'], 'entry_price': pb['price'],
                                        'coin': pb['qty'], 'invested': pb['cost'],
                                        'entry_ts': pb['ts']})
                st['trades'].append({'ts': now_iso(), 'side': 'BUY', 'symbol': pb['symbol'],
                                     'price': pb['price'], 'coin': pb['qty'],
                                     'idr': pb['cost'], 'reason': 'score_top (fill terkonfirmasi)',
                                     'orderId': pb['orderId']})
                report.setdefault('buys_confirmed', []).append(pb['symbol'])
                st['pending_buys'].remove(pb)
            else:
                st['pending_buys'].remove(pb)  # canceled/expired
        elif status not in ('NEW', 'unknown'):
            st['pending_buys'].remove(pb)

    # ---- 2) reconcile pending sells ----
    for ps in list(st.get('pending_sells', [])):
        ns = ps['symbol'].replace('/', '')
        base = ps['symbol'].split('/')[0]
        status = open_order_status(ns, ps['orderId'])
        if status == 'filled_or_canceled' and total.get(base, 0.0) < ps['qty'] * 0.999:
            st['trades'].append({'ts': now_iso(), 'side': 'SELL', 'symbol': ps['symbol'],
                                 'price': ps['price'], 'coin': ps['qty'],
                                 'net_idr': round(ps['qty'] * ps['price'] * (1 - FEE_RATE), 2),
                                 'realized_pnl_pct': round((ps['price'] / ps['entry_price'] - 1) * 100, 2),
                                 'reason': ps['reason'] + ' (fill terkonfirmasi)',
                                 'orderId': ps['orderId']})
            report.setdefault('sells_confirmed', []).append(ps['symbol'])
            st['pending_sells'].remove(ps)
        elif status not in ('NEW', 'unknown'):
            st['pending_sells'].remove(ps)

    # ---- 3) EXIT check posisi aktif ----
    exited = []
    for pos in list(st.get('positions', [])):
        sym = pos['symbol']
        try:
            t = ex.fetch_ticker(sym)
            last = float(t['last'])
            bid = float(t['bid'] or last)
            pnl = (last / pos['entry_price'] - 1) * 100
            action = None
            if pnl <= -STOP_PCT:
                action = ('SELL', 'stop_loss')
            elif pnl >= TARGET_PCT:
                action = ('SELL', 'take_profit')
            if action:
                _, reason = action
                ns = sym.replace('/', '')
                qty = float(ex.amount_to_precision(sym, pos['coin']))
                px = float(ex.price_to_precision(sym, bid))
                res = place_order(ns, 'SELL', qty, px)
                if 'orderId' in res:
                    st['pending_sells'].append({'symbol': sym, 'orderId': res['orderId'],
                                                'price': px, 'qty': qty,
                                                'entry_price': pos['entry_price'],
                                                'reason': reason, 'ts': now_iso()})
                    st['positions'].remove(pos)
                    exited.append({'symbol': sym, 'reason': reason, 'price': px,
                                   'pnl_pct': round((px / pos['entry_price'] - 1) * 100, 2),
                                   'orderId': res['orderId']})
                else:
                    report.setdefault('exit_order_errors', []).append({'symbol': sym, 'res': str(res)[:200]})
        except Exception as e:
            report.setdefault('exit_errors', []).append({'symbol': sym, 'err': str(e)[:150]})
    report['exited'] = exited

    # ---- 4) ENTRY kalau kosong & cash cukup ----
    entries = []
    if not st['positions'] and not st['pending_buys'] and not st['pending_sells']:
        free, total = get_balances()  # refresh (bisa berubah setelah exit)
        idr = free.get('IDR', 0.0)
        if idr >= MIN_ORDER:
            try:
                cfg = sb.load_config()
                top = sb.screen_top(cfg)
                best = None
                for item in top:
                    try:
                        df = sb.get_ohlcv(item['symbol'], '1h', 200)
                        tr = sb.compute_ta(df)
                        s, reasons, pr = sb.score_symbol(tr, item)
                        ok, guard = sb.entry_guard(tr, cfg)
                        if ok:
                            best = {'symbol': item['symbol'], 'score': s, 'reasons': reasons}
                            break
                    except Exception:
                        continue
                if best:
                    sym = best['symbol']
                    ns = sym.replace('/', '')
                    t = ex.fetch_ticker(sym)
                    ask = float(t['ask'] or t['last'])
                    invest = idr * 0.98
                    qty = invest * (1 - FEE_RATE) / ask
                    qty = float(ex.amount_to_precision(sym, qty))
                    ask = float(ex.price_to_precision(sym, ask))
                    cost = qty * ask
                    if MIN_ORDER <= cost <= idr:
                        res = place_order(ns, 'BUY', qty, ask)
                        if 'orderId' in res:
                            st['pending_buys'].append({'symbol': sym, 'orderId': res['orderId'],
                                                       'price': ask, 'qty': qty,
                                                       'cost': round(cost, 2), 'ts': now_iso()})
                            entries.append({'symbol': sym, 'price': ask, 'invest': round(cost, 2),
                                            'score': best['score'], 'orderId': res['orderId'],
                                            'status': 'pending_fill'})
                        else:
                            report['entry_order_error'] = str(res)[:200]
                    else:
                        report['entry_skip'] = 'cost %.0f di luar [10rb, %.0f]' % (cost, idr)
                else:
                    report['entry_skip'] = 'tidak ada kandidat lolos guard'
            except Exception as e:
                report['entry_error'] = str(e)[:250]
    report['entries'] = entries
    report['positions'] = st['positions']
    report['pending_buys'] = st['pending_buys']
    report['pending_sells'] = st['pending_sells']

    # ---- 5) equity ----
    free, total = get_balances()
    idr = free.get('IDR', 0.0)
    equity = idr
    for pos in st['positions']:
        try:
            t = ex.fetch_ticker(pos['symbol'])
            equity += pos['coin'] * float(t['last'])
        except Exception:
            equity += pos['invested']
    # hold manual user (BTC) ikut dihitung biar equity = total akun, bukan cuma saldo bot
    if total.get('BTC', 0.0) > 0:
        try:
            t = ex.fetch_ticker('BTC/IDR')
            equity += total['BTC'] * float(t['last'])
        except Exception:
            pass
    start = st.get('start_equity', START_EQUITY)
    report['equity'] = round(equity, 2)
    report['pnl_idr'] = round(equity - start, 2)
    report['pnl_pct'] = round((equity / start - 1) * 100, 2)
    report['last_trades'] = st['trades'][-5:]
    save_state(st)
    return report


if __name__ == '__main__':
    try:
        print(json.dumps(tick(), indent=2, default=str))
    except Exception as e:
        print(json.dumps({'status': 'error', 'type': type(e).__name__, 'msg': str(e)[:300]}, default=str))
