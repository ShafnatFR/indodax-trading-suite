"""
Backtest jujur: replikasi logika ASLI strategy_bot.py (score_symbol + entry_guard + eval_exit)
vs varian perbaikan (cooldown, trailing lebih lebar, target lebih tinggi, hard_stop ketat, frekuensi turun).

Cara kerja:
- Replay candle per koin secara INDEPENDEN (sama seperti bot: tiap koin dievaluasi sendiri).
- Entry tiap N candle (tick_interval) — bukan tiap candle, mendekati tick ±9 menit / 4 jam.
- Harga eksekusi = close candle. Fee 0.3% per sisi. Modal 500rb, posisi = risk_pct x equity.
- Exit: cut_loss / trailing / target+momentum / timeout — persis eval_exit asli (dengan MA slope, StochRSI).
- Evaluasi di data 1h (60 hari) DAN 4h (60 hari), fee konservatif.

Universe: BTC, ETH, SOL (likuid, baseline) + koin kecil dari laporan (SKR, ZORA, POND, HONEY, UAI, HART, HYPE, SKYAI, BICO, XRP).
"""
import ccxt, pandas as pd, pandas_ta as ta, json, time, os, sys
from datetime import datetime, timezone

FEE = 0.003
START = 500000.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COINS = ['BTC/IDR', 'ETH/IDR', 'SOL/IDR', 'SKR/IDR', 'ZORA/IDR', 'POND/IDR',
         'HONEY/IDR', 'UAI/IDR', 'HART/IDR', 'HYPE/IDR', 'SKYAI/IDR', 'BICO/IDR', 'XRP/IDR']


def fetch_df(sym, tf, limit):
    ex = ccxt.indodax({'enableRateLimit': True})
    ex.load_markets()
    bars = ex.fetch_ohlcv(sym, tf, limit=limit)
    df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.ta.rsi(length=14, append=True)
    df.ta.stochrsi(length=14, append=True)
    df.ta.sma(length=20, append=True, col_names=('MA20',))
    df.ta.sma(length=50, append=True, col_names=('MA50',))
    df.ta.bbands(length=20, std=2, append=True)
    return df


def compute_ta_row(df, i):
    """compute_ta asli, tapi untuk baris i (bukan hanya baris terakhir)."""
    r = {}
    row = df.iloc[i]
    n = i + 1
    if n >= 30 and 'RSI_14' in df.columns and pd.notna(row['RSI_14']):
        r['rsi'] = float(row['RSI_14'])
        r['stoch_k'] = float(row['STOCHRSIk_14_14_3_3']) if pd.notna(row['STOCHRSIk_14_14_3_3']) else None
        r['stoch_d'] = float(row['STOCHRSId_14_14_3_3']) if pd.notna(row['STOCHRSId_14_14_3_3']) else None
    else:
        r['rsi'] = r['stoch_k'] = r['stoch_d'] = None
    if n >= 20:
        r['ma20'] = float(row['MA20']) if pd.notna(row['MA20']) else None
        r['ma50'] = float(row['MA50']) if 'MA50' in df.columns and pd.notna(row['MA50']) else None
        if n >= 25 and 'MA20' in df.columns and pd.notna(df.iloc[i - 5]['MA20']):
            r['ma20_slope'] = (float(row['MA20']) - float(df.iloc[i - 5]['MA20'])) / float(df.iloc[i - 5]['MA20']) * 100
        else:
            r['ma20_slope'] = 0.0
        bb = 'BBU_20_2.0'
        r['bb_upper'] = float(row[bb]) if bb in df.columns and pd.notna(row[bb]) else None
    else:
        r['ma20'] = r['ma50'] = r['ma20_slope'] = r['bb_upper'] = None
    r['last'] = float(row['close'])
    win = df['volume'].iloc[max(0, i - 19):i + 1]
    r['vol_ratio'] = float(row['volume']) / float(win.mean()) if len(win) >= 20 and win.mean() > 0 else 1.0
    return r


def score_symbol(ta_r):
    """score_symbol asli (tanpa summ range — backtest tidak punya summaries harian)."""
    last = ta_r['last']
    s = 0.0
    slope = ta_r.get('ma20_slope')
    if slope is not None:
        s += 25 if slope > 0 else 5
    elif ta_r.get('ma50') and last > ta_r['ma50']:
        s += 20
    else:
        s += 8
    ma20 = ta_r.get('ma20')
    if ma20:
        ratio = last / ma20
        if 1.00 <= ratio <= 1.05:
            s += 30
        elif 1.05 < ratio <= 1.10:
            s += 20
        elif 1.10 < ratio <= 1.20:
            s += 10
        elif ratio > 1.25:
            s += 0
        elif ratio < 1.00:
            s += 10 if (slope or 0) > 0 else 0
    else:
        s += 12
    rsi = ta_r.get('rsi')
    if rsi is not None:
        if 40 <= rsi <= 60:
            s += 20
        elif 60 < rsi <= 70:
            s += 15
        elif rsi > 75:
            s += 3
        elif 30 <= rsi < 40:
            s += 15
        else:
            s += 8
    else:
        s += 10
    k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
    s += 10 if (k is not None and d is not None and k > d) else (3 if (k is not None and d is not None) else 5)
    s += 5 if ta_r.get('vol_ratio', 1.0) >= 1.2 else 1
    return round(min(s, 100), 1)


def entry_guard(ta_r, cfg):
    ma20 = ta_r.get('ma20')
    if ma20 and ta_r['last'] / ma20 > 1 + cfg.get('parabolic_threshold_pct', 25) / 100:
        return False
    if cfg.get('bollinger_upper_avoid', True) and ta_r.get('bb_upper') is not None:
        if ta_r['last'] > ta_r['bb_upper']:
            return False
    return True


def backtest_coin(df, cfg):
    """
    Replay 1 koin. Kembalikan (pnl_pct, n_trades, max_dd_pct, avg_win, avg_loss, n_win, n_loss).
    cfg: min_target_pct, hard_stop_loss_pct, trailing_stop_pct, max_hold_days,
         risk_pct, tick_interval (candle per cek), cooldown_candles, score_min.
    """
    cash = START
    coin = 0.0
    entry_price = None
    high_water = None
    entry_i = None
    exit_blocked_until = -1  # index candle; re-entry diblokir sampai candle ini
    n_trades = 0
    n_win = n_loss = 0
    total_win = total_loss = 0.0
    peak = START
    max_dd = 0.0
    last_scores = {}

    for i in range(len(df)):
        row = df.iloc[i]
        close = float(row['close'])
        if coin > 0:
            pnl = (close / entry_price - 1) * 100
            high_water = max(high_water, close)
            action = None
            reason = ''
            if pnl <= -cfg['hard_stop_loss_pct']:
                action, reason = 'SELL', 'cut_loss'
            elif cfg['trailing_stop_pct'] > 0 and high_water > entry_price:
                if close <= high_water * (1 - cfg['trailing_stop_pct'] / 100):
                    action, reason = 'SELL', 'trailing'
            if not action:
                held = (df['ts'].iloc[i] - df['ts'].iloc[entry_i]).total_seconds() / 86400
                if held >= cfg['max_hold_days']:
                    action, reason = 'SELL', 'timeout'
            if not action and pnl >= cfg['min_target_pct']:
                ta_r = compute_ta_row(df, i)
                slope = ta_r.get('ma20_slope')
                uptrend = (slope is not None and slope > 0) or (ta_r.get('ma50') and close > ta_r['ma50'])
                k, d = ta_r.get('stoch_k'), ta_r.get('stoch_d')
                momentum_weak = (k is not None and d is not None and k < d) or close < (ta_r.get('ma20') or close)
                if uptrend and not momentum_weak:
                    action, reason = 'HOLD', 'target-hold'
                else:
                    action, reason = 'SELL', 'target-sell'
            if action == 'SELL':
                gross = coin * close
                net = gross - gross * FEE
                cash += net
                realized = net - (coin * entry_price / (1 - FEE))  # investasi asli
                pct = realized / (coin * entry_price / (1 - FEE)) * 100
                n_trades += 1
                if pct >= 0:
                    n_win += 1; total_win += pct
                else:
                    n_loss += 1; total_loss += pct
                if reason == 'cut_loss':
                    exit_blocked_until = i + cfg.get('cooldown_candles', 0)
                elif reason == 'trailing':
                    exit_blocked_until = max(exit_blocked_until, i + cfg.get('cooldown_trail_candles', 0))
                coin = 0.0; entry_price = None; high_water = None; entry_i = None
        # ENTRY
        if coin == 0 and cash > 10000 and i >= exit_blocked_until:
            if i % cfg['tick_interval'] == 0:
                ta_r = compute_ta_row(df, i)
                s = score_symbol(ta_r)
                last_scores[df['ts'].iloc[i]] = s
                if s >= cfg.get('score_min', 0) and entry_guard(ta_r, cfg):
                    invest = cash * cfg['risk_pct']
                    if invest < 10000:
                        invest = cash
                    fee = invest * FEE
                    coin = (invest - fee) / close
                    entry_price = close
                    high_water = close
                    entry_i = i
                    cash -= invest
        # equity tracking
        equity = cash + (coin * close if coin > 0 else 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    last_close = float(df.iloc[-1]['close'])
    final = cash + (coin * last_close if coin > 0 else 0)
    pnl = (final / START - 1) * 100
    avg_win = total_win / n_win if n_win else 0.0
    avg_loss = total_loss / n_loss if n_loss else 0.0
    return {'pnl': round(pnl, 2), 'trades': n_trades, 'max_dd': round(max_dd, 2),
            'win': n_win, 'loss': n_loss, 'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
            'final': round(final, 2)}


CONFIGS = {
    'asli (1h, tick 9m≈6h)': dict(min_target_pct=2.0, hard_stop_loss_pct=5.0, trailing_stop_pct=4.0,
                                   max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=0,
                                   cooldown_trail_candles=0, score_min=0),
    'patch A (cooldown 48h)': dict(min_target_pct=2.0, hard_stop_loss_pct=5.0, trailing_stop_pct=4.0,
                                   max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=48,
                                   cooldown_trail_candles=0, score_min=0),
    'patch B (+trail 7%)': dict(min_target_pct=2.0, hard_stop_loss_pct=5.0, trailing_stop_pct=7.0,
                                max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=48,
                                cooldown_trail_candles=0, score_min=0),
    'patch C (+target 5%)': dict(min_target_pct=5.0, hard_stop_loss_pct=5.0, trailing_stop_pct=7.0,
                                 max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=48,
                                 cooldown_trail_candles=0, score_min=0),
    'patch D (+hardstop 3.5)': dict(min_target_pct=5.0, hard_stop_loss_pct=3.5, trailing_stop_pct=7.0,
                                    max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=48,
                                    cooldown_trail_candles=0, score_min=0),
    'patch E (risk 5%, max2 posisi)': dict(min_target_pct=5.0, hard_stop_loss_pct=3.5, trailing_stop_pct=7.0,
                                           max_hold_days=5, risk_pct=0.05, tick_interval=6, cooldown_candles=48,
                                           cooldown_trail_candles=0, score_min=0),
    'patch F (frekuensi 4h)': dict(min_target_pct=5.0, hard_stop_loss_pct=3.5, trailing_stop_pct=7.0,
                                   max_hold_days=5, risk_pct=0.10, tick_interval=24, cooldown_candles=48,
                                   cooldown_trail_candles=0, score_min=0),
    'patch G (score>=65)': dict(min_target_pct=5.0, hard_stop_loss_pct=3.5, trailing_stop_pct=7.0,
                                max_hold_days=5, risk_pct=0.10, tick_interval=6, cooldown_candles=48,
                                cooldown_trail_candles=0, score_min=65),
}


def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else '1h'
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    out_file = os.path.join(BASE_DIR, f'backtest_fix_{tf}_{limit}.json')

    print(f'Fetch {len(COINS)} koin tf={tf} limit={limit} ...', flush=True)
    dfs = {}
    for sym in COINS:
        try:
            dfs[sym] = fetch_df(sym, tf, limit)
            print(f'  {sym}: {len(dfs[sym])} candle', flush=True)
        except Exception as e:
            print(f'  {sym}: GAGAL {str(e)[:120]}', flush=True)
        time.sleep(1)

    agg = {}
    per_coin = {}
    for name, cfg in CONFIGS.items():
        rows = []
        for sym, df in dfs.items():
            try:
                r = backtest_coin(df, cfg)
                rows.append((sym, r))
            except Exception as e:
                print(f'  BT error {name} {sym}: {str(e)[:100]}', flush=True)
        per_coin[name] = rows
        pnls = [r['pnl'] for _, r in rows]
        trades = sum(r['trades'] for _, r in rows)
        wins = sum(r['win'] for _, r in rows)
        losses = sum(r['loss'] for _, r in rows)
        dd = max((r['max_dd'] for _, r in rows), default=0)
        avg_win = sum(r['avg_win'] * r['win'] for _, r in rows) / wins if wins else 0
        avg_loss = sum(r['avg_loss'] * r['loss'] for _, r in rows) / losses if losses else 0
        agg[name] = {'avg_pnl': round(sum(pnls) / len(pnls), 2), 'median_pnl': round(sorted(pnls)[len(pnls) // 2], 2),
                     'n_positif': sum(1 for p in pnls if p > 0), 'n_negatif': sum(1 for p in pnls if p < 0),
                     'total_trades': trades, 'win': wins, 'loss': losses,
                     'winrate': round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
                     'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
                     'payoff': round(avg_win / abs(avg_loss), 2) if avg_loss else 0,
                     'max_dd': round(dd, 2), 'expectancy': round(avg_win * wins / (wins + losses) - abs(avg_loss) * losses / (wins + losses), 2) if (wins + losses) else 0}

    with open(out_file, 'w') as f:
        json.dump({'tf': tf, 'limit': limit, 'agg': agg, 'per_coin': per_coin}, f, indent=2, default=str)

    print(f'\n=== BACKTEST tf={tf} limit={limit} ({len(dfs)} koin) — PnL rata-rata per koin ===')
    print(f"{'konfigurasi':<26} | {'avg%':>6} {'med%':>6} {'pos/neg':>7} {'trades':>6} {'WR%':>5} {'avgW':>6} {'avgL':>6} {'payoff':>6} {'expect':>7} {'maxDD':>6}")
    for name, a in agg.items():
        print(f"{name:<26} | {a['avg_pnl']:>5.2f}% {a['median_pnl']:>5.2f}% {a['n_positif']}/{a['n_negatif']:>3} {a['total_trades']:>6} {a['winrate']:>5.1f} {a['avg_win']:>5.2f} {a['avg_loss']:>6.2f} {a['payoff']:>6.2f} {a['expectancy']:>7.2f} {a['max_dd']:>5.1f}%")
    print(f'\nHasil lengkap per koin: {out_file}')


if __name__ == '__main__':
    main()
