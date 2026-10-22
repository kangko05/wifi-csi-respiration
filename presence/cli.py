"""`python -m presence <command> <session_dir>`

replay      the night through the Presence judge; one JSON line per step on stdout
check       score the replay against the markers; exit 1 when a segment is under 80 %
thresholds  write the thresholds of the empty-home interval to `--out` for `live`
live        judge the session the recorder is writing; ticks go to presence_ticks.jsonl

Thresholds come from the empty-home interval: `--empty HH:MM:SS-HH:MM:SS` on the session's
first day in `--tz`, or else the first `외출` marker segment without its first 90 s (the
marker is pressed on the way out).
"""

import argparse
import datetime as dt
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from breathing.markers import read_markers
from breathing.scoring import JudgeConfig
from breathing.sources import read_ari2_recording
from presence.check import SETTLE_S, score, segments
from presence.features import StreamWindow, stream_windows
from presence.judge import ROOMS, HomeTick, PresenceConfig, PresenceJudge, RoomThresholds, RoomWindow
from presence.live import load_thresholds, run_live, save_thresholds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m presence")
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("replay", "check", "thresholds", "live"):
        command = commands.add_parser(name)
        command.add_argument("session_dir", type=Path)
        command.add_argument("--tz", default="+09:00", help="UTC offset of clock times (default +09:00)")
        command.add_argument("--k", type=float, default=PresenceConfig.k)
        command.add_argument("--empty-minutes", type=float, default=PresenceConfig.empty_minutes)
        command.add_argument("--person-tick-max", type=int, default=PresenceConfig.person_tick_max)
        command.add_argument("--move-motion-ratio", type=float, default=PresenceConfig.move_motion_ratio)
        command.add_argument("--strong-line-ratio", type=float, default=PresenceConfig.strong_line_ratio)
        command.add_argument("--band", type=float, nargs=2, metavar=("LOW", "HIGH"),
                             help="breathing band in /min for the Main-path line (default from the Breathing module)")
        command.add_argument("--home-rule", choices=("line", "motion"), default=PresenceConfig.home_rule,
                             help="person evidence besides Case-link motion: a Main-path line (night) or Main-path"
                                  " motion while home, with Case-link walking the only way back in (doll demo)")

        if name == "live":
            command.add_argument("--thresholds", type=Path, required=True, help="file from `thresholds --out`")
        else:
            command.add_argument("--empty", help="empty-home intervals HH:MM:SS-HH:MM:SS[,HH:MM:SS-HH:MM:SS...] for the thresholds")

        if name == "thresholds":
            command.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    breathing = JudgeConfig() if args.band is None else replace(JudgeConfig(), breathing_band_rpm=(args.band[0], args.band[1]))
    config = replace(PresenceConfig(), breathing=breathing, k=args.k, empty_minutes=args.empty_minutes,
                     person_tick_max=args.person_tick_max, move_motion_ratio=args.move_motion_ratio,
                     strong_line_ratio=args.strong_line_ratio, home_rule=args.home_rule)
    tz = _timezone(args.tz)

    if args.command == "live":
        thresholds = load_thresholds(args.thresholds)
        _print_thresholds(thresholds, None, tz)
        run_live(args.session_dir, config, thresholds)
        return 0

    windows = room_windows(args.session_dir, config)
    times = sorted(windows)

    if not times:
        print(f"no scorable window in {args.session_dir}", file=sys.stderr)
        return 2

    markers = read_markers(args.session_dir)
    intervals = _empty_intervals(args.empty, markers, times, tz)

    if not intervals:
        print("no empty-home interval: pass --empty or mark 외출 and 귀가", file=sys.stderr)
        return 2

    thresholds = thresholds_from(windows, intervals, config)
    _print_thresholds(thresholds, intervals, tz)

    if args.command == "thresholds":
        save_thresholds(thresholds, args.out)
        print(f"wrote {args.out}", file=sys.stderr)
        return 0

    judge = PresenceJudge(config, thresholds)
    ticks = [judge.step(t, windows[t]) for t in times]

    if args.command == "replay":
        for tick in ticks:
            print(tick.to_json())

        _print_counts(ticks)
        return 0

    return _check(ticks, markers, tz)


def room_windows(session_dir: Path, config: PresenceConfig) -> dict[float, dict[int, RoomWindow]]:
    """Step end -> room -> windows, the six streams decoded in parallel."""
    streams = [(room, node) for room in config.rooms for node in (config.case_node, config.main_node)]

    with ProcessPoolExecutor(len(streams)) as pool:
        futures = {s: pool.submit(_stream, session_dir, *s, config) for s in streams}
        by_stream = {s: {w.end: w for w in f.result()} for s, f in futures.items()}

    ends = sorted(set().union(*by_stream.values()))
    return {end: {room: RoomWindow(case=by_stream[room, config.case_node].get(end),
                                   main=by_stream[room, config.main_node].get(end)) for room in config.rooms}
            for end in ends}


def _stream(session_dir: Path, room: int, node: int, config: PresenceConfig) -> list[StreamWindow]:
    records = read_ari2_recording(session_dir, room, node)
    return list(stream_windows(records, config.breathing, config.motion_rate_hz, config.where_window_s))


def thresholds_from(windows: dict[float, dict[int, RoomWindow]], intervals: list[tuple[float, float]],
                    config: PresenceConfig) -> dict[int, RoomThresholds]:
    inside = [w for t, w in windows.items()
              if any(start <= t - config.breathing.window_s and t <= end for start, end in intervals)]
    present = [room for room in config.rooms if any(w[room].case is not None or w[room].main is not None for w in inside)]
    return {room: RoomThresholds.from_windows((w[room] for w in inside), config) for room in present}


def _empty_intervals(text: str | None, markers, times: list[float], tz: dt.timezone) -> list[tuple[float, float]]:
    if text is not None:
        return [_clock_interval(part, times[0], tz) for part in text.split(",")]

    away = next((s for s in segments(markers, times[-1], tz) if s.label == "외출"), None)
    return [] if away is None else [(away.start + SETTLE_S, away.end)]


def _clock_interval(text: str, first_time: float, tz: dt.timezone) -> tuple[float, float]:
    day = dt.datetime.fromtimestamp(first_time, tz).date()
    start, end = (dt.datetime.combine(day, dt.time.fromisoformat(hms), tz).timestamp() for hms in text.split("-"))

    while end < first_time:
        start += 86400.0
        end += 86400.0

    return start, end


def _check(ticks: list[HomeTick], markers, tz: dt.timezone) -> int:
    failed = 0
    print(f"segment                  from      to        ticks  home  rooms  where  per room (first {SETTLE_S:.0f} s skipped)")

    for segment in segments(markers, ticks[-1].time, tz):
        result = score(segment, ticks)
        failed += not result.passed
        per_room = "  ".join(f"{ROOMS[r]} {s:.0%}" for r, s in result.room_shares.items())
        where = "   - " if result.where_share is None else f"{result.where_share:5.0%}"
        print(f"{'PASS' if result.passed else 'FAIL'} {segment.label:<6} {segment.expect.home.value:<10}"
              f" {_fmt(segment.start, tz)}  {_fmt(segment.end, tz)}  {result.ticks:5d}  {result.home_share:4.0%}"
              f"  {result.room_share:5.0%}  {where}  {per_room}")

    return 1 if failed else 0


def _print_thresholds(thresholds: dict[int, RoomThresholds], intervals: list[tuple[float, float]] | None,
                      tz: dt.timezone) -> None:
    origin = "file" if intervals is None else ",".join(f"{_fmt(a, tz)}-{_fmt(b, tz)}" for a, b in intervals)
    print(f"thresholds from {origin}:", file=sys.stderr)

    for room, t in thresholds.items():
        print(f"  room {room} {ROOMS[room]}: case motion > {t.motion.threshold:.5f} (median {t.motion.median:.5f},"
              f" {len(t.motion.scores)} windows), line score > {t.line.threshold:.2f}"
              f" (median {t.line.median:.2f}, {len(t.line.scores)} windows)"
              + ("" if t.main_motion is None else f", main motion > {t.main_motion.threshold:.5f}"
                 f" (median {t.main_motion.median:.5f})"), file=sys.stderr)


def _print_counts(ticks: list[HomeTick]) -> None:
    home = Counter(t.home.value for t in ticks)
    print(f"{len(ticks)} ticks, home {dict(home)}", file=sys.stderr)

    for room in ROOMS:
        print(f"  room {room}: {dict(Counter(t.room(room).state.value for t in ticks))}", file=sys.stderr)


def _timezone(text: str) -> dt.timezone:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hours), minutes=int(minutes)))


def _fmt(time_s: float, tz: dt.timezone) -> str:
    return dt.datetime.fromtimestamp(time_s, tz).strftime("%H:%M:%S")
