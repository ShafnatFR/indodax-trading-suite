#!/usr/bin/env python3
"""
Indodax Technical Analyst — READ-ONLY. Tidak pernah membuat order.
Tiap dipanggil (cron 15-30 menit):
  1. Ambil top-30 volume + skor (pakai engine strategy_bot).
  2. Baca posisi virtual saat ini (PAPER state, kalau ada).
  3. Keluarkan rekomendasi: entry kandidat, exit/hold posisi, regime pasar.
Output: JSON ringkas ke stdout — di-capture cron, dirangkum ke user.

Penting: impor strategy_bot HANYA untuk fungsi baca (get_summaries, score_symbol).
Tidak memanggil tick/reset — tidak menyentuh state bot yang sedang disimulasikan.
"""
import sys, os, json, io
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Redirect strategy_bot import noise ke buffer biar stdout bersih untuk JSON
_buf = io.StringIO()
_old = sys.stdout
sys.stdout = _buf
try:
    import strategy_bot as sb
finally:
    sys.stdout = _old

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'


def analyze():
    cfg = sb.load_config()
    state = sb.load_state()
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')

    # 1. Summaries + scoring (jalur sama persis dengan engine: OHLCV -> TA -> score)
    summ = sb.get_summaries()
    top30 = sb.screen_top(cfg)
    scored = []
    for item in top30:
        key = item['key']
        try:
            ticker = summ.get(key, {})
            df = sb.get_ohlcv(item['symbol'], tf='1h', limit=200)
            ta_r = sb.compute_ta(df)
            score, reasons, pos_range = sb.score_symbol(ta_r, ticker)
            scored.append({
                'key': key,
                'symbol': item['symbol'],
                'last': ta_r.get('last'),
                'score': score,
                'reasons': reasons,
                'range_pos': round(pos_range, 2),
                'rsi': ta_r.get('rsi'),
                'ma20_slope': ta_r.get('ma20_slope'),
                'ma20': ta_r.get('ma20'),
                'guard_ok': sb.entry_guard(ta_r, cfg)[0],
                'guard_reason': sb.entry_guard(ta_r, cfg)[1],
            })
        except Exception as e:
            scored.append({'key': key, 'symbol': item['symbol'], 'error': str(e)[:100]})
    scored.sort(key=lambda x: x.get('score', -1), reverse=True)

    # 2. Posisi virtual dari state (kalau ada sim yang jalan)
    positions = []
    if state and state.get('positions'):
        # harga terakhir tiap posisi dari summaries
        for p in state['positions']:
            key = sb.sym_to_key(p['symbol'])
            t = summ.get(key, {})
            last = float(t.get('last') or 0)
            entry = p.get('entry_price') or 0
            pnl = (last / entry - 1) * 100 if entry else 0
            positions.append({
                'symbol': p['symbol'],
                'entry': entry,
                'last': last,
                'pnl_pct': round(pnl, 2),
                'high_water': p.get('high_water'),
            })
        positions.sort(key=lambda x: x['pnl_pct'])

    # 3. Ringkas rekomendasi (rule-based, konsisten dengan engine)
    recs = []
    for s in scored[:5]:
        recs.append({
            'symbol': s['symbol'],
            'score': s.get('score'),
            'last': s.get('last'),
            'why': s.get('reasons', [])[:3],
            'guard_ok': s.get('guard_ok'),
            'guard_reason': s.get('guard_reason'),
        })

    return {
        'ts': ts,
        'mode': cfg.get('mode'),
        'n_scored': len(scored),
        'top_entry_candidates': recs,
        'open_positions_virtual': positions,
        'config': {
            'risk_per_trade_pct': cfg.get('risk_per_trade_pct'),
            'min_target_pct': cfg.get('min_target_pct'),
            'hard_stop_loss_pct': cfg.get('hard_stop_loss_pct'),
            'trailing_stop_pct': cfg.get('trailing_stop_pct'),
            'max_positions': cfg.get('max_positions'),
        },
    }


if __name__ == '__main__':
    try:
        print(json.dumps(analyze(), ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
