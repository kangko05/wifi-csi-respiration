"""One hillclimb measurement: judge config overrides -> doll metrics (10-16) + night metrics (labelled).

usage: python tools/sweep_config.py --night <session_dir> --baseline <json> [--sleep HH:MM-HH:MM] [--empty HH:MM-HH:MM]
                                   [--set key=value ...] [--tag name] [--no-doll]
Tuples are given as "a,b". Prints one JSON row and appends it to docs/reports/night1019/sweep.jsonl.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from breathing import Judge, JudgeConfig, QuietBaseline, read_ari2_recording  # noqa: E402
import night_score  # noqa: E402
import score_doll  # noqa: E402

KST = timezone(timedelta(hours=9))


def parse_set(config: JudgeConfig, items: list[str]) -> JudgeConfig:
    changes = {}
    for item in items:
        key, value = item.split("=", 1)
        current = getattr(config, key)
        if isinstance(current, tuple):
            changes[key] = tuple(float(v) for v in value.split(","))
        elif isinstance(current, int) and not isinstance(current, bool):
            changes[key] = int(value)
        else:
            changes[key] = float(value)
    return dataclasses.replace(config, **changes)


def night(session: Path, baseline_path: Path, config: JudgeConfig, sleep: list[str], empty: list[str]) -> dict:
    quiet = QuietBaseline.from_json(baseline_path.read_text())
    quiet = dataclasses.replace(quiet, k=config.baseline_k, min_ratio=config.baseline_min_ratio)
    ticks = [json.loads(t.to_json()) for t in Judge(config, quiet).run(read_ari2_recording(session, 1, 2))]
    argv = [str(session / "ticks_sweep.jsonl"), str(session / "markers.txt"), "--json"]
    for s in sleep:
        argv += ["--sleep", s]
    for e in empty:
        argv += ["--empty", e]
    (session / "ticks_sweep.jsonl").write_text("\n".join(json.dumps(t) for t in ticks) + "\n", encoding="utf-8")
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        night_score.main(argv)
    result = json.loads(out.getvalue())
    result["threshold"] = round(quiet.threshold, 3)
    result["ticks"] = len(ticks)
    return result


def main(argv: list[str]) -> int:
    args = iter(argv)
    session = baseline_path = None
    sets, sleep, empty = [], [], []
    tag, do_doll = "", True
    for a in args:
        if a == "--night":
            session = Path(next(args))
        elif a == "--baseline":
            baseline_path = Path(next(args))
        elif a == "--set":
            sets.append(next(args))
        elif a == "--sleep":
            sleep.append(next(args))
        elif a == "--empty":
            empty.append(next(args))
        elif a == "--tag":
            tag = next(args)
        elif a == "--no-doll":
            do_doll = False
    config = parse_set(JudgeConfig(), sets)
    row = {"tag": tag, "set": sets, "at": datetime.now(KST).strftime("%m-%d %H:%M")}
    if do_doll:
        row["doll"] = score_doll.score(ROOT / "recordings/meeting_20261016_102602", config)
    if session is not None and baseline_path is not None:
        row["night"] = night(session, baseline_path, config, sleep, empty)
    line = json.dumps(row, ensure_ascii=False)
    print(line)
    with open(ROOT / "docs/reports/night1019/sweep.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
