#!/bin/bash
# Simulasi PAPER sampai 05:00 WIB besok (deadline-based), data pasar realtime
# Tik tiap ~9 menit (540 detik), state di strategy_state.json
cd ~/Development/indodax-bot || exit 1
PY="/c/Users/shafnats/AppData/Local/Programs/Python/Python312/python.exe"
LOG="sim_until_5am.log"
DEADLINE=$(date -d "tomorrow 05:00" +%s)
: > "$LOG"
echo "START $(date '+%Y-%m-%d %H:%M:%S %Z') | deadline=$(date -d @$DEADLINE '+%F %T %Z')" >> "$LOG"
"$PY" strategy_bot.py status >> "$LOG" 2>&1
"$PY" analyst_bot.py > analyst_snapshot_000.json 2>/dev/null || true
i=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  i=$((i+1))
  echo "=== TICK $i $(date '+%H:%M:%S') ===" >> "$LOG"
  "$PY" strategy_bot.py tick >> "$LOG" 2>&1
  "$PY" strategy_bot.py status >> "$LOG" 2>&1
  N=$(printf "%03d" "$i")
  "$PY" analyst_bot.py > "analyst_snapshot_${N}.json" 2>/dev/null || true
  NOW=$(date +%s)
  REMAIN=$((DEADLINE - NOW))
  if [ "$REMAIN" -gt 540 ]; then sleep 540; elif [ "$REMAIN" -gt 0 ]; then sleep "$REMAIN"; fi
done
echo "=== DONE $(date '+%Y-%m-%d %H:%M:%S %Z') ===" >> "$LOG"
