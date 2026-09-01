#!/usr/bin/env python3
"""
Indodax Hybrid Bot — engine rule + AI advisor (LLM via 9router).

Arsitektur:
  - Engine rule: SAMA dengan strategy_bot (SCREEN -> SCORE -> ENTRY -> EXIT).
  - AI advisor: sebelum entry, bot minta opini LLM (via 9router OpenAI-compatible
    endpoint) dengan konteks kandidat + posisi. LLM jawab APPROVE / VETO + alasan.
  - Risk layer: cut-loss, trailing, timeout TIDAK BISA di-veto AI (keamanan).

Mode:
  - PAPER (default) — state terpisah: hybrid_state.json
  - LIVE — terkunci (butuh env + konfirmasi), belum diimplementasikan penuh

State terpisah dari strategy_state.json → bisa A/B test dengan mode BOT murni.
"""
import ccxt
import pandas as pd
import pandas_ta as ta
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'indodax_bot_config.json')
STATE_FILE = os.path.join(BASE_DIR, 'hybrid_state.json')

# LLM advisor config (9router lokal)
LLM_URL = os.environ.get('IDX_LLM_URL', 'http://localhost:20128/v1/chat/completions')
LLM_MODEL = os.environ.get('IDX_LLM_MODEL', 'Ika')
# API key 9router diambil dari sqlite (lokal) atau env IDX_LLM_KEY
LLM_KEY = os.environ.get('IDX_LLM_KEY', '')

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
    r = subprocess.run(['curl', '-4', '-sS', '-A', UA, url],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'curl fail: {r.stderr[:200]}')
    return json.loads(r.stdout)


def sym_to_key(symbol):
    return symbol.lower().replace('/', '_')


def key_to_sym(key):
    return key.upper().replace('_', '/')


def fmt_idr(v):
    return f'Rp {v:,.0f}'


def get_9router_key():
    """Ambil API key 9router dari sqlite lokal (nama 'Hermes')."""
    if LLM_KEY:
        return LLM_KEY
    try:
        import sqlite3
        db = os.path.expandvars(r'%APPDATA%\9router\db\data.sqlite')
        if not os.path.exists(db):
            db = os.path.join(os.environ.get('APPDATA', ''), '9router', 'db', 'data.sqlite')
        con = sqlite3.connect(db)
        cur = con.cursor()
        r = cur.execute("SELECT key FROM apiKeys WHERE name='Hermes'").fetchone()
        con.close()
        if r:
            return r[0]
    except Exception as e:
        print(f'[hybrid] warning: gagal ambil key 9router: {e}', file=sys.stderr)
    return ''


def llm_advise(prompt, timeout=120, retries=3):
    """Panggil LLM via 9router dengan retry + jeda. Return (text, error)."""
    key = get_9router_key()
    if not key:
        return '', 'no_llm_key'
    body = json.dumps({
        'model': LLM_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'temperature': 0.2,
        'max_tokens': 300,
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
    })
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            text = data['choices'][0]['message']['content']
            if text and text.strip():
                return text, None
            last_err = 'empty_response'
        except Exception as e:
            last_err = str(e)[:200]
        time.sleep(3 * (attempt + 1))
    return '', last_err


# ------------------------------------------------------------------ market (sama dgn strategy_bot)
def get_summaries():
    return curl_json('https://indodax.com/api/summaries')['tickers']


def get_ohlcv(symbol, tf='1h', limit=200):
    ex = ccxt.indodax({'enableRateLimit': True})
    bars = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    return df


def compute_ta(df):
    ta_r = {'last': float(df.iloc[-1]['close'])}
    if len(df) >= 20:
        sma20 = ta.sma(df['close'], length=20)
        if sma20 is not None and len(sma20) >= 2:
            ma20 = float(sma20.iloc[-1])
            ma20_prev = float(sma20.iloc[-2])
            ta_r['ma20'] = ma20
            ta_r['ma20_slope'] = (ma20 / ma20_prev - 1) * 100 if ma20_prev else 0
        bbands = ta.bbands(df['close'], length=20, std=2)
        if bbands is not None:
            # nama kolom bervariasi antar versi pandas-ta; cari kolom BBU*
            bbu_col = [c for c in bbands.columns if c.startswith('BBU_')]
            bbl_col = [c for c in bbands.columns if c.startswith('BBL_')]
            if bbu_col:
                ta_r['bb_upper'] = float(bbands[bbu_col[0]].iloc[-1])
            if bbl_col:
                ta_r['bb_lower'] = float(bbands[bbl_col[0]].iloc[-1])
    if len(df) >= 30:
        rsi = ta.rsi(df['close'], length=14)
        if rsi is not None and len(rsi) >= 1:
            ta_r['rsi'] = float(rsi.iloc[-1])
        stoch = ta.stochrsi(df['close'], length=14, k=3, d=3)
        if stoch is not None:
            k_col = [c for c in stoch.columns if c.startswith('STOCHRSIk_')]
            d_col = [c for c in stoch.columns if c.startswith('STOCHRSId_')]
            if k_col:
                ta_r['stoch_k'] = float(stoch[k_col[0]].iloc[-1])
            if d_col:
                ta_r['stoch_d'] = float(stoch[d_col[0]].iloc[-1])
    vol = df['volume'].tail(20)
    ta_r['vol_ratio'] = float(df['volume'].iloc[-1] / vol.mean()) if vol.mean() > 0 else 1.0
    if len(df) >= 50:
        sma50 = ta.sma(df['close'], length=50)
        if sma50 is not None:
            ta_r['ma50'] = float(sma50.iloc[-1])
    return ta_r


def score_symbol(ta_r, summ):
    last = ta_r['last']
    s = 0.0
    reasons = []
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
    k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
    if k is not None and d is not None:
        s += 10 if k > d else 3
        reasons.append('stoch_bull' if k > d else 'stoch_bear')
    else:
        s += 5; reasons.append('stoch_bootstrap')
    vr = ta_r.get('vol_ratio', 1.0)
    s += 5 if vr >= 1.2 else 1
    reasons.append('vol_hi' if vr >= 1.2 else 'vol_low')
    high = float(summ.get('high') or 0); low = float(summ.get('low') or 0)
    pos_range = (last - low) / (high - low) if high > low else 0.5
    if 0.2 <= pos_range <= 0.6:
        s += 10; reasons.append('range_mid')
    elif pos_range < 0.2:
        s += 6; reasons.append('range_low')
    else:
        s += 3; reasons.append('range_high')
    return round(min(s, 100), 1), reasons, pos_range


def screen_top(config):
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


def entry_guard(ta_r, config):
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
    return equity * config.get('risk_per_trade_pct', 10) / 100


def open_position(st, symbol, price, invest_idr, fee_rate=0.003):
    coin = invest_idr * (1 - fee_rate) / price
    st['positions'].append({
        'symbol': symbol, 'entry_price': price, 'coin': coin, 'invested': invest_idr,
        'entry_ts': now_iso(), 'high_water': price, 'fee_rate': fee_rate,
    })
    st['cash'] -= invest_idr


def eval_exit(pos, ta_r, summ, config):
    last = ta_r['last']
    pnl = (last / pos['entry_price'] - 1) * 100
    high_water = max(pos.get('high_water', pos['entry_price']), last)
    pos['high_water'] = high_water
    # hard stop loss — tidak bisa di-veto
    if pnl <= -config.get('hard_stop_loss_pct', 5):
        return 'SELL', f'cut_loss ({pnl:.2f}%)'
    # trailing stop dari high-water
    if high_water > pos['entry_price']:
        drawdown = (high_water - last) / high_water * 100
        if drawdown >= config.get('trailing_stop_pct', 4):
            return 'SELL', f'trailing ({drawdown:.1f}% dari high {high_water:,.0f})'
    # timeout
    hold_days = (datetime.now(timezone.utc) - datetime.fromisoformat(pos['entry_ts'])).total_seconds() / 86400
    if hold_days >= config.get('max_hold_days', 5):
        return 'SELL', f'timeout ({hold_days:.1f} hari)'
    # target profit
    if pnl >= config.get('min_target_pct', 2):
        # HOLD kalau tren naik & momentum kuat; SELL kalau melemah
        slope = ta_r.get('ma20_slope')
        k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
        if (slope or 0) > 0 and k is not None and d is not None and k > d:
            return 'HOLD', f'target {pnl:.2f}% tercapai, tren naik, tahan'
        return 'SELL', f'target {pnl:.2f}% + momentum melemah'
    return 'HOLD', f'belum target ({pnl:.2f}%)'


# ------------------------------------------------------------------ AI advisor
def ai_advise_entry(candidate, positions, config):
    """Minta opini LLM sebelum entry. Return (approve: bool, reason: str)."""
    pos_txt = '\n'.join(
        f"- {p['symbol']}: entry {p['entry_price']:,.0f}, invested {p['invested']:,.0f}"
        for p in positions) or '(tidak ada posisi)'
    prompt = f"""Kamu adalah technical analyst crypto. Evaluasi ENTRY di exchange Indodax (IDR).

KANDIDAT ENTRY:
- Symbol: {candidate['symbol']}
- Harga: {candidate.get('last', 0):,.0f} IDR
- Skor teknikal (0-100): {candidate.get('score')}
- Alasan skor: {', '.join(candidate.get('reasons', []) or [])}
- RSI14: {candidate.get('rsi')}
- MA20: {candidate.get('ma20'):,.0f} (slope {candidate.get('ma20_slope')}%)
- Posisi range harian: {candidate.get('range_pos')}
- Volume ratio vs 20-bar: {candidate.get('vol_ratio')}
- Guard teknikal: {'OK' if candidate.get('guard_ok') else candidate.get('guard_reason')}

POSISI SAAT INI:
{pos_txt}

Tugas: evaluasi apakah setup teknikal ini layak ENTRY. Fokus pada:
1. Apakah trend mendukung (uptrend, pullback sehat)?
2. Apakah volume mengkonfirmasi?
3. Apakah ada red flag (harga 0, data anomali, overbought ekstrem, falling knife)?
4. Apakah risk/reward masuk akal untuk swing trade 1-5 hari?

Jawab HANYA satu baris: APPROVE <alasan>  atau  VETO <alasan>
Jika data anomali (harga 0, MA20 0, volume 0) → VETO. Jika setup kuat → APPROVE."""
    text, err = llm_advise(prompt)
    if err:
        return None, f'llm_error: {err}'
    up = text.strip().upper()
    if up.startswith('APPROVE'):
        return True, text.strip()
    if up.startswith('VETO'):
        return False, text.strip()
    # coba ambil kata kunci di tengah kalimat
    for word in ('APPROVE', 'VETO'):
        if word in up:
            return (True if word == 'APPROVE' else False), text.strip()
    # debug: simpan prompt + raw utk diagnosa
    try:
        with open(os.path.join(BASE_DIR, 'llm_debug.log'), 'a') as f:
            f.write(f'[{now_iso()}] PROMPT:\n{prompt}\n---\nRAW: {text!r}\nERR: {err}\n=====\n')
    except Exception:
        pass
    # LLM kosong/tidak jelas -> VETO (hati-hati: jangan entry tanpa validasi AI)
    return False, f'llm_empty, veto hati-hati: {text[:80]!r}'


def ai_advise_exit(pos, ta_r, pnl, engine_action, reason, config):
    """AI boleh mengubah HOLD <-> SELL pada kondisi target (bukan cut-loss/trailing)."""
    # Risk layer: cut-loss/trailing/timeout wajib
    if reason.startswith(('cut_loss', 'trailing', 'timeout')):
        return engine_action, reason, f'risk_layer (wajib): {reason}'
    # Target tercapai -> tanya AI
    prompt = f"""Kamu adalah technical analyst crypto. Evaluasi EXIT posisi (PAPER trading).

POSISI: {pos['symbol']}
- Entry: {pos['entry_price']:,.0f} IDR
- Harga sekarang: {ta_r['last']:,.0f} IDR
- PnL saat ini: {pnl:.2f}%
- RSI14: {ta_r.get('rsi')}
- MA20 slope: {ta_r.get('ma20_slope')}%
- StochRSI K/D: {ta_r.get('stoch_k')}/{ta_r.get('stoch_d')}
- High-water: {pos.get('high_water'):,.0f}

Target profit +{config.get('min_target_pct')}% sudah tercapai. Engine menyarankan: {engine_action} ({reason}).

Tugas: putuskan SELL (ambil profit) atau HOLD (biarkan tren lanjut).
Jawab HANYA satu baris: SELL <alasan>  atau  HOLD <alasan>"""
    text, err = llm_advise(prompt)
    if err:
        return engine_action, reason, f'llm_error, ikuti engine: {err}'
    up = text.strip().upper()
    if up.startswith('SELL'):
        return 'SELL', f'ai_sell: {text.strip()[:80]}'
    if up.startswith('HOLD'):
        return 'HOLD', f'ai_hold: {text.strip()[:80]}'
    return engine_action, reason, f'llm_unclear, ikuti engine: {text[:80]}'


# ------------------------------------------------------------------ tick
def tick(config=None):
    config = config or load_config()
    st = load_state()
    if not st:
        st = {'mode': 'HYBRID-PAPER', 'created': now_iso(),
              'cash': float(config.get('start_cash_idr', 500000)),
              'start_cash': float(config.get('start_cash_idr', 500000)),
              'positions': [], 'trades': [], 'ticks': [], 'ai_log': []}
        save_state(st)

    report = {'ts': now_iso(), 'mode': st['mode']}
    exited = []
    summaries = None

    # --- Warm-up LLM: panggil sekali di awal supaya koneksi 9router hangat ---
    if config.get('ai_enabled', True):
        warm, warm_err = llm_advise('Balas satu kata: OK')
        if not warm:
            report['ai_warmup'] = f'llm tidak siap: {warm_err}'

    # --- EXIT (dengan AI advisor hanya untuk target; risk layer wajib) ---
    for pos in list(st['positions']):
        try:
            df = get_ohlcv(pos['symbol'], '1h', 200)
            tr = compute_ta(df)
            if summaries is None:
                summaries = get_summaries()
            summ = summaries.get(sym_to_key(pos['symbol']), {})
            action, reason = eval_exit(pos, tr, summ, config)
            pnl = (tr['last'] / pos['entry_price'] - 1) * 100
            # AI hanya untuk kondisi target (bukan risk layer)
            if action == 'SELL' and reason.startswith('target'):
                action, reason, ai_note = ai_advise_exit(pos, tr, pnl, action, reason, config)
                st['ai_log'].append({'ts': now_iso(), 'type': 'exit', 'symbol': pos['symbol'],
                                     'pnl': round(pnl, 2), 'decision': action, 'note': ai_note})
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
                                     'reason': reason,
                                     'hold_days': round((datetime.now(timezone.utc) - datetime.fromisoformat(pos['entry_ts'])).total_seconds()/86400, 2)})
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

    # --- SCORE ---
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

    # --- ENTRY: engine pilih kandidat, lalu AI approve/veto ---
    new_entries = []
    ai_empty_streak = 0
    ai_allowance = int(config.get('ai_allowance', 2))
    if len(st['positions']) < config.get('max_positions', 5):
        for c in scored:
            if 'error' in c or not c.get('guard_ok'):
                continue
            if len(new_entries) >= config.get('max_new_positions_per_tick', 1):
                break
            invest = position_size(config, st['cash'] + sum(p['invested'] for p in st['positions']))
            if invest < 10000:
                break
            # AI advisor (dengan jeda antar panggilan utk hindari rate-limit 9router)
            ai_ok, ai_note = ai_advise_entry(c, st['positions'], config)
            time.sleep(1.5)
            st['ai_log'].append({'ts': now_iso(), 'type': 'entry', 'symbol': c['symbol'],
                                 'score': c['score'], 'decision': 'APPROVE' if ai_ok else ('VETO' if ai_ok is False else 'NO_LLM'),
                                 'note': ai_note})
            if ai_ok is False and ai_note.startswith(('llm_empty', 'llm_error')):
                # LLM gagal -> allowance: kalau sudah N streak, fallback ke engine
                ai_empty_streak += 1
                if ai_empty_streak > ai_allowance:
                    report.setdefault('ai_fallback', []).append({'symbol': c['symbol'], 'reason': ai_note})
                    ai_ok = True  # fallback engine (jangan blokir total)
                    ai_note = f'fallback_engine (LLM down {ai_empty_streak}x): {ai_note}'
                else:
                    continue
            elif ai_ok is False:
                ai_empty_streak = 0
                report.setdefault('ai_vetoes', []).append({'symbol': c['symbol'], 'score': c['score'], 'reason': ai_note})
                continue  # veto -> coba kandidat berikutnya
            else:
                ai_empty_streak = 0
            # approve (atau fallback engine) -> entry
            open_position(st, c['symbol'], c['last'], invest)
            st['trades'].append({'ts': now_iso(), 'side': 'BUY', 'symbol': c['symbol'],
                                 'price': c['last'], 'idr': invest, 'reason': 'score_top_ai',
                                 'score': c['score']})
            new_entries.append({'symbol': c['symbol'], 'price': c['last'], 'invest': round(invest, 2),
                                'score': c['score'], 'ai': ai_note})
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


# ------------------------------------------------------------------ status
def status_report():
    st = load_state()
    if not st:
        return {'status': 'no state'}
    equity = st.get('equity_last') or st.get('cash')
    ticks = st.get('ticks', [])
    last = ticks[-1] if ticks else {}
    return {
        'status': 'ok', 'mode': st.get('mode'), 'cash': round(st.get('cash', 0), 2),
        'equity': last.get('equity'), 'pnl_pct': last.get('pnl_pct'),
        'positions_open': len(st.get('positions', [])),
        'positions': st.get('positions', []),
        'n_trades': len(st.get('trades', [])),
        'ai_log': st.get('ai_log', [])[-10:],
        'trades': st.get('trades', []),
        'start_cash': st.get('start_cash'),
    }


def reset():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return {'status': 'reset ok'}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Indodax Hybrid Bot (AI advisor + rule engine)')
    ap.add_argument('cmd', choices=['tick', 'status', 'reset'])
    args = ap.parse_args()
    if args.cmd == 'tick':
        print(json.dumps(tick(), ensure_ascii=False, indent=2))
    elif args.cmd == 'status':
        print(json.dumps(status_report(), ensure_ascii=False, indent=2))
    elif args.cmd == 'reset':
        print(json.dumps(reset(), ensure_ascii=False))


if __name__ == '__main__':
    main()
