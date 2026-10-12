"""Collector: read the boards, record, and (optionally) judge breathing and apnea live.

    python tools/collect.py record --port /dev/ttyUSB0 [--duration 3600]
    python tools/collect.py live   --port /dev/ttyUSB0 [--room 1 --node 2]
    python tools/collect.py hold   --port /dev/ttyUSB0 [--count 7 --hold-s 20 --period-s 120]

record  only records (default output folder recordings/<mode>_<start time>/); a status
        line every --status-every seconds shows packets/s per (room, node), seconds since
        the last packet and bytes skipped, so a dead link is seen at once.
live    record + Monitor on one (room, node) stream: breathing rate every --rate-every
        seconds, apnea start/end at once. A start is reported as apnea only when the
        rate judge saw breathing in the 120 s before it; otherwise it is logged as ignored.
hold    live + a breath-hold schedule with beeps (long beep = hold, short beep = breathe).
        At the end the failed holds are entered by number and the score is printed.

Recording is on by default (--no-record turns it off). --replay <chunk.ari.gz ...> reads
recorded files instead of serial ports (live and hold judge them as if live, as fast as
read). --radar-port is reserved for the LD6002 reference radar and not implemented yet.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ari2 import MAIN_PATH_NODE, parse  # noqa: E402
from holds import Beeper, HoldSchedule, score  # noqa: E402
from recorder import ChunkRecorder, TextLog, read_records  # noqa: E402
from respiration import RATE, Monitor  # noqa: E402

DEFAULT_ROOM = 1
DEFAULT_NODE = MAIN_PATH_NODE


def clock(t_s: float) -> str:
    return datetime.fromtimestamp(t_s).strftime("%H:%M:%S")


class Status:
    """Packet counts per (room, node) and the time of the last packet, printed every status_every seconds."""

    def __init__(self, status_every_s: float, printer):
        self.status_every_s = status_every_s
        self.printer = printer
        self.counts = Counter()
        self.last_seen = {}
        self.period_start = None
        self.next_print = None

    def count(self, key: tuple[int, int], t_s: float) -> None:
        self.counts[key] += 1
        self.last_seen[key] = t_s

    def maybe_print(
        self, now_s: float, extra: str = "", skipped_bytes: int = 0
    ) -> None:
        if self.next_print is None:
            self.period_start = now_s
            self.next_print = now_s + self.status_every_s

        if now_s < self.next_print:
            return

        span = max(now_s - self.period_start, 1e-9)
        parts = []

        for key in sorted(self.last_seen):
            rate = self.counts[key] / span
            idle = now_s - self.last_seen[key]
            parts.append(f"room{key[0]}/node{key[1]} {rate:5.1f}/s idle {idle:4.1f}s")

        if not parts:
            parts.append("no packets")

        self.printer(
            f"[{clock(now_s)}] "
            + " | ".join(parts)
            + f" | skipped {skipped_bytes} B"
            + extra
        )

        self.counts.clear()
        self.period_start = now_s
        self.next_print = now_s + self.status_every_s


class LiveJudge:
    """Monitor on one stream; prints the rate periodically and apnea events as they come."""

    def __init__(
        self,
        room: int,
        node: int,
        rate_every_s: float,
        printer,
        events_log: TextLog | None,
    ):
        self.key = (room, node)
        self.rate_every_s = rate_every_s
        self.printer = printer
        self.events_log = events_log
        self.monitor = Monitor(log=printer)
        self.next_rate = None
        self.apnea_starts: list[float] = (
            []
        )  # confirmed starts (rate judge saw breathing)
        self.ignored_starts: list[float] = []

    def push(self, room: int, node: int, frame) -> None:
        if (room, node) != self.key:
            return

        for event in self.monitor.push(frame):
            self._report(event)

        if self.next_rate is None:
            self.next_rate = frame.t_s + self.rate_every_s

        if frame.t_s >= self.next_rate:
            self.next_rate = frame.t_s + self.rate_every_s
            self._report_rate(frame.t_s)

    def _report_rate(self, t_s: float) -> None:
        result = self.monitor.rate()

        if result.state == RATE:
            text = f"rate {result.rate_per_min:.1f}/min ({result.signal}, line x{result.peak:.1f})"
        else:
            text = f"rate unknown ({result.reason})"

        self._emit(t_s, f"{text} | state {self.monitor.state}")

    def _report(self, event) -> None:
        if event.kind == "end":
            self._emit(
                event.t,
                f"apnea end (started {clock(event.apnea.start)}, {event.t - event.apnea.start:.0f} s)",
            )
            return

        before = self.monitor.rate(event.t)

        if before.state == RATE:
            self.apnea_starts.append(event.t)
            self._emit(
                event.t,
                f"APNEA start (stopped {clock(event.apnea.start)}; breathing before: {before.rate_per_min:.1f}/min)",
            )
        else:
            self.ignored_starts.append(event.t)
            self._emit(event.t, f"start ignored (no breathing before: {before.reason})")

    def _emit(self, t_s: float, text: str) -> None:
        self.printer(f"[{clock(t_s)}] {text}")

        if self.events_log is not None:
            self.events_log.write(text, t_s)


class HoldSession:
    """Runs the beep schedule against the wall clock and writes the schedule rows."""

    def __init__(
        self,
        schedule: HoldSchedule,
        start_s: float,
        beeper: Beeper,
        printer,
        log: TextLog | None,
    ):
        self.schedule = schedule
        self.start_s = start_s
        self.beeper = beeper
        self.printer = printer
        self.log = log
        self.pending = []

        for index, (a, b) in enumerate(schedule.holds()):
            self.pending.append((start_s + a, index, "start"))
            self.pending.append((start_s + b, index, "end"))

        self.pending.sort()

    def tick(self, now_s: float) -> None:
        while self.pending and now_s >= self.pending[0][0]:
            when, index, kind = self.pending.pop(0)

            if kind == "start":
                self.beeper.beep(seconds=0.8, frequency_hz=660.0)
                self.printer(
                    f"[{clock(when)}] HOLD {index + 1}/{self.schedule.count}: stop breathing"
                )
            else:
                self.beeper.beep(seconds=0.25, frequency_hz=1100.0)
                self.printer(f"[{clock(when)}] hold {index + 1} over: breathe")

            if self.log is not None:
                self.log.write(
                    f"hold {index} {kind} planned_s={when - self.start_s:.1f}", when
                )

    @property
    def done(self) -> bool:
        return not self.pending

    def holds_wall_s(self) -> list[tuple[float, float]]:
        return [(self.start_s + a, self.start_s + b) for a, b in self.schedule.holds()]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=["record", "live", "hold"])
    parser.add_argument(
        "--port",
        action="append",
        default=[],
        help="serial port of a board (repeatable)",
    )
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument(
        "--replay",
        nargs="+",
        default=None,
        help="recorded .ari.gz chunks instead of ports",
    )
    parser.add_argument(
        "--radar-port",
        default=None,
        help="LD6002 reference radar (not implemented yet)",
    )
    parser.add_argument(
        "--out", default=None, help="output folder (default recordings/<mode>_<time>)"
    )
    parser.add_argument(
        "--no-record", action="store_true", help="do not write packet chunks"
    )
    parser.add_argument(
        "--duration", type=float, default=None, help="stop after this many seconds"
    )
    parser.add_argument("--status-every", type=float, default=10.0)
    parser.add_argument(
        "--room", type=int, default=DEFAULT_ROOM, help="live/hold: stream room"
    )
    parser.add_argument(
        "--node",
        type=int,
        default=DEFAULT_NODE,
        help="live/hold: stream node (1 in-case, 2 main path)",
    )
    parser.add_argument(
        "--rate-every",
        type=float,
        default=30.0,
        help="live/hold: seconds between rate lines",
    )
    parser.add_argument("--first-hold-s", type=float, default=100.0)
    parser.add_argument("--hold-s", type=float, default=20.0)
    parser.add_argument("--period-s", type=float, default=120.0)
    parser.add_argument("--count", type=int, default=7)

    args = parser.parse_args(argv)

    if args.radar_port is not None:
        parser.error("--radar-port: LD6002 input is not implemented yet")

    if not args.port and args.replay is None:
        parser.error("give at least one --port, or --replay")

    return args


def packet_source(args, printer):
    """Yields (arrival_ns, source, payload); from serial ports or from recorded chunks."""
    if args.replay is not None:

        def replay():
            for path in args.replay:
                yield from read_records(Path(path))

        return replay(), []

    from serial_input import open_readers

    incoming, readers = open_readers(args.port, printer, args.baud)

    def live():
        while True:
            try:
                yield incoming.get(timeout=0.5)
            except queue.Empty:
                yield None

    return live(), readers


def main(argv=None) -> int:
    args = parse_args(argv)
    start_s = time.time()

    out = (
        Path(args.out)
        if args.out
        else Path("recordings")
        / f"{args.mode}_{datetime.fromtimestamp(start_s):%Y%m%d_%H%M%S}"
    )
    out.mkdir(parents=True, exist_ok=True)

    session_log = TextLog(out / "session.txt")

    def printer(text: str) -> None:
        print(text, flush=True)
        session_log.write(text)

    printer(f"mode {args.mode}; output {out}; ports {args.port or args.replay}")

    recorder = None
    if not args.no_record and args.replay is None:
        recorder = ChunkRecorder(out)

    judge = None
    if args.mode in ("live", "hold"):
        judge = LiveJudge(
            args.room, args.node, args.rate_every, printer, TextLog(out / "events.txt")
        )

    schedule = None
    holds = None
    if args.mode == "hold":
        schedule = HoldSchedule(
            args.first_hold_s, args.hold_s, args.period_s, args.count
        )
        printer(
            f"holds: first at +{schedule.first_hold_s:.0f} s, {schedule.hold_s:.0f} s long, every {schedule.period_s:.0f} s, x{schedule.count}"
        )

    status = Status(args.status_every, printer)
    source, readers = packet_source(args, printer)

    # The session clock: wall clock live, the recorded clock in replay (set by the first packet).
    session_start_s = None if args.replay is not None else start_s
    last_wall_s = start_s

    try:
        for item in source:
            now_s = time.time()

            if item is not None:
                arrival_ns, source_index, payload = item

                if recorder is not None:
                    recorder.write(arrival_ns, source_index, payload)

                packet = parse(payload, arrival_ns)

                if packet is not None:
                    status.count((packet.room, packet.node), packet.frame.t_s)
                    last_wall_s = packet.frame.t_s

                    if session_start_s is None:
                        session_start_s = packet.frame.t_s

                    if judge is not None:
                        judge.push(packet.room, packet.node, packet.frame)

            # In replay the recorded clock drives the schedule and the status; live, the wall clock.
            clock_s = last_wall_s if args.replay is not None else now_s

            if session_start_s is None:
                continue

            if schedule is not None and holds is None:
                holds = HoldSession(
                    schedule,
                    session_start_s,
                    Beeper(out, enabled=args.replay is None),
                    printer,
                    TextLog(out / "schedule.txt"),
                )

            if holds is not None:
                holds.tick(clock_s)

            skipped = sum(reader.skipped_bytes for reader in readers)
            extra = f" | state {judge.monitor.state}" if judge is not None else ""
            status.maybe_print(clock_s, extra, skipped)

            if (
                holds is not None
                and holds.done
                and clock_s >= session_start_s + holds.schedule.total_s
            ):
                printer("schedule finished")
                break

            if args.duration is not None and clock_s - session_start_s >= args.duration:
                printer(f"duration {args.duration:.0f} s reached")
                break
    except KeyboardInterrupt:
        printer("stopped by user")
    finally:
        for reader in readers:
            reader.stop()

        if recorder is not None:
            recorder.close()

    if holds is not None and judge is not None:
        report_hold_score(holds, judge, printer, out)

    session_log.close()

    return 0


def report_hold_score(holds: HoldSession, judge: LiveJudge, printer, out: Path) -> None:
    answer = ""

    if sys.stdin.isatty():
        answer = input(
            "failed holds (numbers as shown, space separated; empty = none): "
        ).strip()

    failed = {int(x) - 1 for x in answer.split() if x.isdigit()}
    (out / "failed.txt").write_text(" ".join(str(i + 1) for i in sorted(failed)) + "\n")

    result = score(judge.apnea_starts, holds.holds_wall_s(), failed)

    printer(
        f"score: {result['hits']}/{result['valid_holds']} holds detected, "
        f"{len(result['false_alarms'])} false alarms, "
        f"{len(judge.ignored_starts)} starts ignored by the rate check"
    )

    if result["delays_s"]:
        delays = ", ".join(f"{d:.1f}" for d in result["delays_s"])
        printer(f"delays after the hold start [s]: {delays}")

    if result["missed"]:
        printer("missed holds: " + ", ".join(str(i + 1) for i in result["missed"]))

    for start in result["false_alarms"]:
        printer(f"false alarm at {clock(start)}")


if __name__ == "__main__":
    sys.exit(main())
