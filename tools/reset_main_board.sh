#!/bin/bash
# Main board stalled (recorder shows "watchdog restart: no packets for 30 s" and reopening does not help).
# Runbook step: stop the recorder (one process owns the port), pulse the reset line, start the recorder
# again on the same session so the live judges keep tailing the same folder.
# usage: bash tools/reset_main_board.sh [SESSION_DIR] [LOG]
set -u
cd "$(dirname "$0")/.."
BOARD=A2DF0F2C38BDF011B7C5137148E9DE0F
SESSION=${1:-recordings/20261019_192030_night-20261019}
LOG=${2:-docs/reports/night1019/recorder.log}

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'record_main' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force; 'stopped ' + \$_.ProcessId }"
sleep 2
python -X utf8 tools/board_reset.py "$BOARD"
nohup python -X utf8 tools/record_main.py "$BOARD" night-20261019 --session "$SESSION" >> "$LOG" 2>&1 &
echo "recorder restarted $(date +%H:%M:%S); judgments return in about 60 s"
