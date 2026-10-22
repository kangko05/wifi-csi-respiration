"""`python -m breathing <command> ...`

replay        a recording through the judge; ticks as JSON lines on stdout
baseline      a Quiet baseline from a recording interval, written as JSON
baseline-live a Quiet baseline from the live input for N minutes
live          the demo display tailing a session that the recorder is writing
live-frames   the demo display on a csi-link RX port (no recording)
check         the marker-based replay check: PASS/FAIL per marker, exit 1 on any FAIL

Times on the command line are HH:MM:SS on the recording's first day in `--tz`. Without
`--baseline-file`, the live commands load `baselines/<room>_<node>.json` (`frames.json`
for a port) when it exists and say "no baseline" otherwise.
"""

import argparse
import datetime as dt
import statistics
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from breathing.check import baseline_interval, check_ticks
from breathing.display import REFRESH_S, KeyInput, LiveView, Screen, colours, render
from breathing.judge import Judge, State
from breathing.markers import BASELINE_END, BASELINE_START, read_markers, write_marker
from breathing.scoring import JudgeConfig, QuietBaseline, window_scores
from breathing.sources import CsiRecord, live_frames, read_ari2_recording, read_frames_recording, tail_ari2_recording

BASELINE_DIR = Path(__file__).resolve().parent.parent / "baselines"
FRAMES_BASELINE = "frames.json"
TICKS_FILE = "ticks.jsonl"
NO_INPUT_NOTICE_S = 5.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m breathing")
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="replay a recording through the judge")
    _add_source_options(replay)
    replay.add_argument("--from", dest="start", metavar="HH:MM:SS", help="first record to judge (default: recording start)")
    replay.add_argument("--to", dest="end", metavar="HH:MM:SS", help="end of the replay (default: recording end)")
    replay.add_argument("--baseline-file", type=Path, help="JSON written by a baseline command")

    baseline = commands.add_parser("baseline", help="compute a Quiet baseline and write it as JSON")
    _add_source_options(baseline)
    baseline.add_argument("--out", type=Path, required=True)

    baseline_live = commands.add_parser("baseline-live", help="Quiet baseline from the live input")
    baseline_live.add_argument("session_dir", type=Path, nargs="?", help="session the recorder is writing")
    baseline_live.add_argument("--port", help="csi-link RX port instead of a session")
    _add_stream_options(baseline_live)
    baseline_live.add_argument("--minutes", type=float, default=3.0)
    baseline_live.add_argument("--out", type=Path, help=f"default {BASELINE_DIR}/<room>_<node>.json")

    live = commands.add_parser("live", help="demo display tailing the recorder's session")
    live.add_argument("session_dir", type=Path)
    _add_stream_options(live)
    _add_display_options(live)

    frames = commands.add_parser("live-frames", help="demo display on a csi-link RX port, not recorded")
    frames.add_argument("port", help="COM name or USB serial of the RX board")
    frames.add_argument("--log-dir", type=Path, default=Path("."), help="where ticks.jsonl and markers.txt go")
    _add_display_options(frames)

    check = commands.add_parser("check", help="marker-based replay check of a session")
    _add_source_options(check)
    check.set_defaults(source="ari2")
    check.add_argument("--baseline-file", type=Path, help="default: the session's 기준선 시작/끝 markers")

    for command in (replay, baseline, baseline_live, live, frames, check):
        _add_judge_options(command)

    args = parser.parse_args(argv)
    config = _judge_config(args)

    if args.command == "baseline-live":
        return _baseline_live(args, config)

    if args.command == "live":
        return _live(args, config)

    if args.command == "live-frames":
        return _live_frames(args, config)

    if args.command == "check":
        return _check(args, config)

    return _replay_or_baseline(args, config)


def _replay_or_baseline(args: argparse.Namespace, config: JudgeConfig) -> int:
    records = list(_read(args))

    if not records:
        print("no records", file=sys.stderr)
        return 1

    tz = _timezone(args.tz)
    clock = _clock_on(records[0].time, tz)

    if args.command == "baseline":
        quiet = _baseline_from(records, args.baseline, clock, config)
        args.out.write_text(quiet.to_json())
        print(f"baseline windows={len(quiet.scores)} median={quiet.median:.3f} mad={quiet.mad:.3f} "
              f"threshold={quiet.threshold:.3f} -> {args.out}", file=sys.stderr)
        return 0

    if args.baseline_file is not None:
        quiet = QuietBaseline.from_json(args.baseline_file.read_text())
    elif args.baseline is not None:
        quiet = _baseline_from(records, args.baseline, clock, config)
    else:
        quiet = None

    start = clock(args.start) if args.start else records[0].time
    end = clock(args.end) if args.end else records[-1].time + 1.0
    ticks = []
    previous = None
    _print_threshold(quiet)

    for tick in Judge(config, quiet).run(r for r in records if start <= r.time < end):
        print(tick.to_json())
        ticks.append(tick)

        if tick.state is not previous:
            print(f"{_fmt(tick.time, tz)} {tick.state.value} ({tick.reason})", file=sys.stderr)
            previous = tick.state

    for state in State:
        own = [t for t in ticks if t.state is state]
        scores = [t.score for t in own if t.score is not None]
        median = f"{statistics.median(scores):.2f}" if scores else "-"
        print(f"{state.value}: {len(own)} ticks ({len(own) / max(len(ticks), 1):.0%}), median score {median}", file=sys.stderr)

    return 0


def _baseline_live(args: argparse.Namespace, config: JudgeConfig) -> int:
    if args.port is not None:
        from breathing.ports import open_port

        records = live_frames(open_port(args.port))
        out = args.out or BASELINE_DIR / FRAMES_BASELINE
        marker_dir = None
    elif args.session_dir is not None:
        if not args.session_dir.is_dir():
            print(f"no such session: {args.session_dir}", file=sys.stderr)
            return 2

        records = tail_ari2_recording(args.session_dir, args.room, args.node, since=time.time())
        out = args.out or BASELINE_DIR / f"{args.room}_{args.node}.json"
        marker_dir = args.session_dir
    else:
        print("baseline-live needs a session_dir or --port", file=sys.stderr)
        return 2

    deadline = time.time() + args.minutes * 60.0
    last_notice = time.time()
    scores = []
    print(f"Quiet baseline for {args.minutes:g} min; keep the room still (doll off)", file=sys.stderr)

    if marker_dir is not None:
        write_marker(marker_dir, BASELINE_START)

    def until_deadline() -> Iterator[CsiRecord]:
        nonlocal last_notice

        for item in records:
            now = time.time()

            if now >= deadline:
                return

            if item is None and now - last_notice >= NO_INPUT_NOTICE_S:
                print(f"{_fmt(now)} no input", file=sys.stderr)
                last_notice = now
            elif item is not None:
                last_notice = now
                yield item

    try:
        for window in window_scores(until_deadline(), config):
            scores.append(window)
            print(f"{_fmt(window.end)} window {len(scores)} score={window.score:.2f} "
                  f"peak={window.peak_rpm:.1f}/min fill={window.fill:.2f} motion={window.motion:.2f}%", file=sys.stderr)
    except KeyboardInterrupt:
        print("stopped early", file=sys.stderr)

    if marker_dir is not None:
        write_marker(marker_dir, BASELINE_END)

    if not scores:
        print("no window scored: no input, or less than a window of records", file=sys.stderr)
        return 1

    quiet = QuietBaseline.from_windows(scores, config)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(quiet.to_json())
    print(f"baseline windows={len(quiet.scores)} median={quiet.median:.3f} mad={quiet.mad:.3f} "
          f"threshold={quiet.threshold:.3f} -> {out}", file=sys.stderr)
    return 0


def _live(args: argparse.Namespace, config: JudgeConfig) -> int:
    if not args.session_dir.is_dir():
        print(f"no such session: {args.session_dir}", file=sys.stderr)
        return 2

    quiet, note = _load_baseline(args.baseline_file, BASELINE_DIR / f"{args.room}_{args.node}.json")
    records = tail_ari2_recording(args.session_dir, args.room, args.node, since=time.time() - config.window_s)
    title = f"live  {args.session_dir.name}  room {args.room} node {args.node}"
    return _run_live(records, Judge(config, quiet), args.session_dir, title, note, args.debug, args.ticks_file)


def _live_frames(args: argparse.Namespace, config: JudgeConfig) -> int:
    from breathing.ports import open_port

    connection = open_port(args.port)
    quiet, note = _load_baseline(args.baseline_file, BASELINE_DIR / FRAMES_BASELINE)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    title = f"live-frames  {connection.port}  (not recorded)"
    return _run_live(live_frames(connection), Judge(config, quiet), args.log_dir, title, note, args.debug)


def _run_live(records: Iterator[CsiRecord | None], judge: Judge, log_dir: Path, title: str, baseline_note: str,
              debug: bool, ticks_file: str = TICKS_FILE) -> int:
    """The display loop: every record to the judge, every tick to ticks.jsonl, a redraw per
    REFRESH_S, typed markers to markers.txt."""
    screen = Screen()
    keys = KeyInput()
    colour = colours()
    shown_state = judge.state
    state_since = time.time()
    last_tick = None
    last_draw = time.time()  # the first frame waits a refresh so the backlog is judged first
    count = 0
    count_since = time.time()
    packets_per_s = 0.0
    last_marker = ""

    with open(log_dir / ticks_file, "a", encoding="utf-8") as log:
        try:
            for item in records:
                now = time.time()

                if item is None:
                    tick = judge.heartbeat(now)
                else:
                    count += 1
                    tick = judge.feed(item)

                if tick is not None:
                    log.write(tick.to_json() + "\n")
                    log.flush()
                    last_tick = tick

                if judge.state is not shown_state:
                    shown_state = judge.state
                    state_since = now

                for text in keys.poll():
                    marker = write_marker(log_dir, text)
                    last_marker = f"{_fmt(marker.time)} {text}"

                if now - last_draw < REFRESH_S:
                    continue

                packets_per_s = count / max(now - count_since, 1e-9)
                count = 0
                count_since = now
                times, waveform = judge.window_waveform()
                view = LiveView(title=title, clock=_fmt(now), state=judge.state, since_change_s=now - state_since,
                                tick=last_tick, threshold=None if judge.baseline is None else judge.baseline.threshold,
                                baseline_note=baseline_note, input_ok=judge.input_ok(now), input_age_s=judge.input_age(now),
                                packets_per_s=packets_per_s, waveform_times=times, waveform=waveform,
                                window_s=judge.config.window_s, input_buffer=keys.buffer, last_marker=last_marker,
                                debug=debug)
                screen.show(render(view, colour))
                last_draw = now
        except KeyboardInterrupt:
            pass

    return 0


def _check(args: argparse.Namespace, config: JudgeConfig) -> int:
    records = list(_read(args))
    markers = read_markers(args.session_dir)

    if not records:
        print("no records", file=sys.stderr)
        return 1

    if args.baseline_file is not None:
        quiet = QuietBaseline.from_json(args.baseline_file.read_text())
    elif (interval := baseline_interval(markers)) is not None:
        first, last = interval
        quiet = QuietBaseline.from_records((r for r in records if first <= r.time < last), config)
    else:
        print("no --baseline-file and no 기준선 시작/끝 markers", file=sys.stderr)
        return 2

    _print_threshold(quiet)
    ticks = list(Judge(config, quiet).run(records))
    results = check_ticks(ticks, markers, config)

    for result in results:
        print(f"{result.verdict} {_fmt(result.marker_time)} {result.label}: {result.detail}")

    if not results:
        print("no 멈춤 / 재개 / 인형 켬 markers in markers.txt", file=sys.stderr)
        return 1

    return 0 if all(r.passed for r in results) else 1


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--source", choices=("frames", "ari2"), default="frames")
    parser.add_argument("--room", type=int, default=1, help="ari2 stream room (default 1)")
    parser.add_argument("--node", type=int, default=2, help="ari2 stream node (default 2, the Main path)")
    parser.add_argument("--baseline", metavar="HH:MM:SS-HH:MM:SS", help="Quiet baseline interval inside the recording")
    parser.add_argument("--tz", default="+09:00", help="UTC offset of the clock times (default +09:00)")


def _add_stream_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--room", type=int, default=1, help="ari2 stream room (default 1)")
    parser.add_argument("--node", type=int, default=2, help="ari2 stream node (default 2, the Main path)")


def _add_judge_options(parser: argparse.ArgumentParser) -> None:
    low, high = JudgeConfig.breathing_band_rpm
    parser.add_argument("--band", type=float, nargs=2, metavar=("LOW", "HIGH"),
                        help=f"breathing band in /min (default {low:g} {high:g}); the doll demo uses 10 20 because an empty"
                             " lab room showed a 23-24/min floor line (2026-10-21)")


def _judge_config(args: argparse.Namespace) -> JudgeConfig:
    config = JudgeConfig()
    return config if args.band is None else replace(config, breathing_band_rpm=(args.band[0], args.band[1]))


def _add_display_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-file", type=Path, help="JSON written by a baseline command")
    parser.add_argument("--debug", action=argparse.BooleanOptionalAction, default=False,
                        help="show the spectral peak and last breath line (hidden by default)")
    parser.add_argument("--ticks-file", default=TICKS_FILE,
                        help=f"tick log name inside the session (default {TICKS_FILE}; a second judge on another stream needs its own)")


def _load_baseline(explicit: Path | None, default: Path) -> tuple[QuietBaseline | None, str]:
    path = explicit if explicit is not None else default

    if explicit is None and not path.exists():
        return None, "no baseline"

    quiet = QuietBaseline.from_json(path.read_text())
    return quiet, f"baseline {path.name}: {len(quiet.scores)} windows, median {quiet.median:.2f}"


def _print_threshold(quiet: QuietBaseline | None) -> None:
    if quiet is not None:
        print(f"threshold={quiet.threshold:.3f} (median {quiet.median:.3f} + {quiet.k:g} * mad {quiet.mad:.3f}, "
              f"{len(quiet.scores)} windows)", file=sys.stderr)


def _read(args: argparse.Namespace) -> Iterator[CsiRecord]:
    if args.source == "ari2":
        return read_ari2_recording(args.session_dir, args.room, args.node)

    return read_frames_recording(args.session_dir)


def _timezone(text: str) -> dt.timezone:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hours), minutes=int(minutes)))


def _clock_on(anchor: float, tz: dt.timezone):
    """HH:MM:SS -> unix seconds on the day of `anchor` in `tz`."""
    day = dt.datetime.fromtimestamp(anchor, tz).date()

    def clock(hms: str) -> float:
        return dt.datetime.combine(day, dt.time.fromisoformat(hms), tz).timestamp()

    return clock


def _fmt(time_s: float, tz: dt.timezone | None = None) -> str:
    """HH:MM:SS in `tz`, or in the local zone (the clock the markers carry)."""
    return dt.datetime.fromtimestamp(time_s, tz).strftime("%H:%M:%S")


def _baseline_from(records: list[CsiRecord], interval: str | None, clock, config: JudgeConfig) -> QuietBaseline:
    if interval is None:
        raise SystemExit("--baseline HH:MM:SS-HH:MM:SS or --baseline-file is required")

    first, last = (clock(part) for part in interval.split("-"))
    return QuietBaseline.from_records((r for r in records if first <= r.time < last), config)
