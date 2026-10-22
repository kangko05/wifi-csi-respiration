"""Night accuracy from judge ticks and labelled segments.

usage: python tools/night_score.py <ticks.jsonl> <markers.txt> [--sleep HH:MM-HH:MM]... [--empty HH:MM-HH:MM]... [--json]

Segments come from markers whose text starts with 취침 / 기상 (sleep) and 외출 / 귀가 (empty home),
or from --sleep / --empty ranges (KST, may cross midnight). A tick counts in a segment when the
window it judged (60 s before the tick) lies inside it. Expected state: sleep -> BREATHING,
empty -> not BREATHING and no line. APNEA starts per sleep hour are reported alongside.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
WINDOW_S = 60.0


def parse_markers(path: Path) -> list[tuple[float, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out.append((int(parts[0]) / 1e9, parts[2].strip()))
    return out


def segments_from_markers(markers, start_words, end_words, last_time):
    segments, open_at = [], None
    for time, text in markers:
        if text.startswith(start_words) and open_at is None:
            open_at = time
        elif text.startswith(end_words) and open_at is not None:
            segments.append((open_at, time))
            open_at = None
    if open_at is not None:
        segments.append((open_at, last_time))
    return segments


def parse_range(text: str, day: datetime) -> tuple[float, float]:
    a, b = text.split("-")
    start = day.replace(hour=int(a[:2]), minute=int(a[3:5]), second=0)
    end = day.replace(hour=int(b[:2]), minute=int(b[3:5]), second=0)
    if end <= start:
        end += timedelta(days=1)
    return start.timestamp(), end.timestamp()


def subtract(segments, holes):
    out = []
    for s, e in segments:
        pieces = [(s, e)]
        for hs, he in holes:
            pieces = [p for a, b in pieces for p in ((a, min(b, hs)), (max(a, he), b)) if p[0] < p[1]]
        out.extend(pieces)
    return out


GRACE_S = 90.0  # a window plus the apnea gap: the judge cannot have reacted to a segment's start before this


def inside(ticks, segments):
    return [t for t in ticks if any(s + GRACE_S <= t["time"] - WINDOW_S and t["time"] <= e for s, e in segments)]


def starts(ticks, state):
    return sum(1 for a, b in zip(ticks, ticks[1:]) if b["state"] == state and a["state"] != state)


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv if not a.startswith("--") and "-" not in a[:3]]
    ticks = [json.loads(l) for l in paths[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    markers = parse_markers(paths[1]) if len(paths) > 1 and paths[1].exists() else []
    last = ticks[-1]["time"] if ticks else 0.0
    day = datetime.fromtimestamp(ticks[0]["time"], KST).replace(microsecond=0) if ticks else datetime.now(KST)
    sleep = segments_from_markers(markers, ("취침",), ("기상",), last)
    empty = segments_from_markers(markers, ("외출",), ("귀가",), last)
    args = iter(argv)
    for a in args:
        if a == "--sleep":
            sleep.append(parse_range(next(args), day))
        elif a == "--empty":
            empty.append(parse_range(next(args), day))
    sleep = subtract(sleep, empty)

    sleep_ticks = inside(ticks, sleep)
    empty_ticks = inside(ticks, empty)
    sleep_hours = sum(e - s for s, e in sleep) / 3600
    result = {
        "sleep_ticks": len(sleep_ticks),
        "sleep_breathing_pct": round(100 * sum(t["state"] == "BREATHING" for t in sleep_ticks) / len(sleep_ticks), 1) if sleep_ticks else None,
        "sleep_line_pct": round(100 * sum(bool(t.get("line")) for t in sleep_ticks) / len(sleep_ticks), 1) if sleep_ticks else None,
        "sleep_apnea_starts_per_h": round(starts(sleep_ticks, "APNEA") / sleep_hours, 2) if sleep_hours else None,
        "empty_ticks": len(empty_ticks),
        "empty_breathing_pct": round(100 * sum(t["state"] == "BREATHING" for t in empty_ticks) / len(empty_ticks), 1) if empty_ticks else None,
        "empty_line_pct": round(100 * sum(bool(t.get("line")) for t in empty_ticks) / len(empty_ticks), 1) if empty_ticks else None,
        "sleep_segments": [(datetime.fromtimestamp(s, KST).strftime("%H:%M"), datetime.fromtimestamp(e, KST).strftime("%H:%M")) for s, e in sleep],
        "empty_segments": [(datetime.fromtimestamp(s, KST).strftime("%H:%M"), datetime.fromtimestamp(e, KST).strftime("%H:%M")) for s, e in empty],
    }
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for key, value in result.items():
            print(f"{key:26} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
