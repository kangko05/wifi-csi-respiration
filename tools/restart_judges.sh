#!/bin/bash
# Restart the live judges and the phone page on the current code, keeping the recorder running.
# Judges tail the recording, so nothing is lost; each judge warms up for about 60 s.
# usage: bash tools/restart_judges.sh [SESSION_DIR] [REPORT_DIR]
set -u
cd "$(dirname "$0")/.."
S=${1:-recordings/20261019_192030_night-20261019}
R=${2:-docs/reports/night1019}

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'breathing live|presence live|presence_bridge|marker_server' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force; 'stopped ' + \$_.ProcessId }"
sleep 2
nohup python -X utf8 -m breathing live $S --room 1 --node 2 --baseline-file $R/baseline_room1_empty_all.json > $R/live_screen.txt 2>> $R/live_err.txt &
nohup python -X utf8 -m breathing live $S --room 2 --node 2 --baseline-file $R/baseline_room2_empty3.json --ticks-file ticks_room2.jsonl > $R/live_screen_room2.txt 2>> $R/live_err_room2.txt &
nohup python -X utf8 -m breathing live $S --room 3 --node 2 --baseline-file $R/baseline_room3_empty3.json --ticks-file ticks_room3.jsonl > $R/live_screen_room3.txt 2>> $R/live_err_room3.txt &
nohup python -X utf8 -m presence live $S --thresholds $R/presence_thresholds.json >> $R/presence_live.log 2>&1 &
nohup python -X utf8 tools/presence_bridge.py $S --server ${VOICE_SERVER:-http://127.0.0.1:8765} >> $R/presence_bridge.log 2>&1 &
nohup python -X utf8 tools/marker_server.py $S --port 8090 --refresh-s 5 >> $R/marker_server.log 2>&1 &
echo "judges and page restarted $(date +%H:%M:%S); judgments return in about 60 s; page http://100.88.133.50:8090/"
