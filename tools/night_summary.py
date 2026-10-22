"""Summarize judge ticks of a night: per-hour state share, line share, score, transitions, markers.

usage: python tools/night_summary.py <ticks.jsonl> [markers.txt] [--tz +09:00]
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


def parse_tz(text: str) -> timezone:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def main(argv: list[str]) -> int:
    tz = parse_tz(argv[argv.index("--tz") + 1]) if "--tz" in argv else parse_tz("+09:00")
    paths = [Path(a) for a in argv if not a.startswith("--") and a not in ("+09:00",)]
    paths = [p for p in paths if p.suffix in (".jsonl", ".txt")]
    ticks = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    markers = []
    if len(paths) > 1 and paths[1].exists():
        for line in paths[1].read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                markers.append((parts[1][11:19], parts[2]))

    def clock(tick: dict) -> datetime:
        return datetime.fromtimestamp(tick["time"], tz)

    by_hour: dict[str, list[dict]] = defaultdict(list)
    for tick in ticks:
        by_hour[clock(tick).strftime("%m-%d %H")].append(tick)

    print("hour         ticks  NO_SIGNAL BREATHING APNEA  line%  score_med  input_ok%")
    for hour, group in sorted(by_hour.items()):
        states = Counter(t["state"] for t in group)
        n = len(group)
        scores = [t["score"] for t in group if t["score"] is not None]
        line = sum(1 for t in group if t.get("line"))
        ok = sum(1 for t in group if t.get("input_ok", True))
        print(f"{hour}  {n:5d}  {100*states['NO_SIGNAL']/n:8.0f}% {100*states['BREATHING']/n:8.0f}% {100*states['APNEA']/n:5.0f}% {100*line/n:5.0f}%  {median(scores) if scores else float('nan'):8.2f}  {100*ok/n:8.0f}%")

    print("\ntransitions:")
    previous = None
    for tick in ticks:
        if tick["state"] != previous:
            print(f"  {clock(tick).strftime('%H:%M:%S')}  {tick['state']:<10} score={tick['score']} thr={tick['threshold']} reason={tick.get('reason', '')}")
            previous = tick["state"]

    if markers:
        print("\nmarkers:")
        for when, text in markers:
            print(f"  {when}  {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
