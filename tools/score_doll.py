"""Doll accuracy on the 10-16 meeting recording (main path, csi-link frames).

usage: python tools/score_doll.py [recordings/meeting_20261016_102602] [--json]
Prints BREATHING share and APNEA count while the doll breathes (11:00-11:06), whether the
pause segment (11:16-11:20) gives APNEA and returns to BREATHING, and the empty-room BREATHING share.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
from breathing import Judge, JudgeConfig, QuietBaseline, State, read_frames_recording

KST = dt.timezone(dt.timedelta(hours=9))


def kst(hms: str) -> float:
    return dt.datetime.fromisoformat(f"2026-10-16T{hms}").replace(tzinfo=KST).timestamp()


EMPTY = (kst("10:27:00"), kst("10:32:00"))
DOLL = (kst("11:00:00"), kst("11:06:00"))
PAUSES = (kst("11:16:00"), kst("11:20:00"))
END = kst("11:21:00")


def inside(ticks, segment, window_s):
    start, end = segment
    return [t for t in ticks if start <= t.time - window_s and t.time <= end]


def share(ticks, state) -> float:
    return sum(t.state is state for t in ticks) / len(ticks) if ticks else float("nan")


def score(session_dir: Path, config: JudgeConfig = JudgeConfig()) -> dict:
    records = list(read_frames_recording(session_dir / "main"))
    baseline = QuietBaseline.from_records((r for r in records if EMPTY[0] <= r.time < EMPTY[1]), config)
    ticks = list(Judge(config, baseline).run(r for r in records if r.time < END))
    doll = inside(ticks, DOLL, config.window_s)
    pauses = inside(ticks, PAUSES, config.window_s)
    empty = [t for t in ticks if t.time <= EMPTY[1] + 60]
    apnea_starts = sum(1 for a, b in zip(doll, doll[1:]) if b.state is State.APNEA and a.state is not State.APNEA)
    returned = any(b.state is State.BREATHING and a.state is State.APNEA for a, b in zip(pauses, pauses[1:]))
    return {
        "threshold": round(baseline.threshold, 3),
        "doll_breathing_pct": round(100 * share(doll, State.BREATHING), 1),
        "doll_apnea_starts": apnea_starts,
        "pause_apnea_seen": any(t.state is State.APNEA for t in pauses),
        "pause_returned_to_breathing": returned,
        "empty_breathing_pct": round(100 * share(empty, State.BREATHING), 1),
        "doll_ticks": len(doll),
    }


def main(argv: list[str]) -> int:
    paths = [a for a in argv if not a.startswith("--")]
    session = Path(paths[0]) if paths else Path("recordings/meeting_20261016_102602")
    result = score(session)
    if "--json" in argv:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key:30} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
