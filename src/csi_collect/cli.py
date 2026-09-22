"""Command line entry point for the raw CSI collector."""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import recorder, serialport

DEFAULT_BAUD = 921600
DEFAULT_IDLE_TIMEOUT_S = 10.0
DEFAULT_SERIAL_TIMEOUT_S = recorder.POLL_INTERVAL_S

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130

REPO_ROOT = Path(__file__).resolve().parents[2]

DESCRIPTION = """\
Record the raw ESP32-C5 RX serial stream into a new session directory.

Every received byte is stored unmodified in serial.bin; chunks.csv and lines.csv
only index those bytes with host receipt times and parse verdicts. No IQ
decoding, filtering or respiration estimation happens here. One explicit port is
opened; no role is guessed and nothing is written to the device.
"""

EPILOG = """\
examples:
  python collect.py --list-ports
  python collect.py --port COM3 --duration 60 --location "옆 교수회의실"

Conditions left unspecified are recorded as unknown (null), not guessed.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect.py",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list-ports", action="store_true", help="list serial ports (no port is opened) and exit")
    parser.add_argument("--port", help="RX serial port to open, e.g. COM3 (required unless --list-ports)")
    parser.add_argument("--duration", type=float, help="capture duration in seconds, finite and positive (required)")
    parser.add_argument("--location", help="measurement location, recorded verbatim (required)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"serial baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_S,
        help=f"fail the capture if no byte arrives for this long (default: {DEFAULT_IDLE_TIMEOUT_S:g}s)",
    )
    parser.add_argument("--data-root", default=str(REPO_ROOT / "data"), help="parent directory for session directories")
    parser.add_argument("--occupancy", help="who is in the room, if known (unset = unknown)")
    parser.add_argument("--posture", help="subject posture, if known (unset = unknown)")
    parser.add_argument("--distance-cm", type=float, help="subject distance in cm, if known (unset = unknown)")
    parser.add_argument("--notes", help="free-form note (unset = unknown, empty string = intentionally empty)")
    return parser


def _pyserial_version() -> str | None:
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        return None
    return getattr(serial, "__version__", "unknown")


def main(argv: list[str] | None = None, *, serial_factory=None, comports=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_ports:
        try:
            ports = serialport.list_ports(comports)
        except serialport.MissingDependency as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        if not ports:
            print("no serial ports found")
        for port in ports:
            print(f"{port['device']}\t{port['description']}\t{port['hwid']}")
        return EXIT_OK

    missing = [name for name in ("port", "duration", "location") if getattr(args, name) in (None, "")]
    if missing:
        parser.error("missing required argument(s): " + ", ".join("--" + name for name in missing))
    for name in ("duration", "idle_timeout"):
        try:
            recorder.require_positive_finite(name, getattr(args, name))
        except ValueError as exc:
            parser.error(str(exc))
    if args.baud <= 0:
        parser.error("--baud must be positive")
    if args.distance_cm is not None:
        try:
            recorder.require_positive_finite("distance_cm", args.distance_cm)
        except ValueError as exc:
            parser.error(str(exc))

    config = {
        "port": args.port,
        "baud": args.baud,
        "requested_duration_s": args.duration,
        "idle_timeout_s": args.idle_timeout,
        "serial_read_timeout_s": DEFAULT_SERIAL_TIMEOUT_S,
        "max_line_bytes": recorder.MAX_LINE_BYTES,
        "read_chunk_bytes": recorder.READ_CHUNK_BYTES,
        "flush_interval_s": recorder.FLUSH_INTERVAL_S,
        "data_root": str(Path(args.data_root).resolve()),
    }
    conditions = {
        "location": args.location,
        "occupancy": args.occupancy,
        "posture": args.posture,
        "distance_cm": args.distance_cm,
        "notes": args.notes,
    }
    host = {
        "command_argv": [Path(sys.argv[0]).name or "collect.py", *argv],
        "cwd": str(Path.cwd()),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "pyserial_version": _pyserial_version(),
        "host_utc_at_start": datetime.now(timezone.utc).isoformat(),
    }

    try:
        session = recorder.create_session(
            Path(args.data_root), config=config, conditions=conditions, host=host
        )
    except OSError as exc:
        print(f"could not create session directory under {args.data_root}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"session directory: {session.path}")

    try:
        handle = serialport.open_serial(
            args.port, args.baud, timeout=DEFAULT_SERIAL_TIMEOUT_S, serial_factory=serial_factory
        )
    except serialport.MissingDependency as exc:
        return _fail(session, str(exc), "missing_dependency")
    except (OSError, ValueError) as exc:
        return _fail(session, f"could not open {args.port}: {exc}", "serial_open_error")
    except KeyboardInterrupt:
        return _fail(session, "interrupted before the port was opened", "keyboard_interrupt")

    try:
        session.info["serial_settings"] = serialport.describe_settings(handle)
        info = session.record(handle, args.duration, args.idle_timeout)
    except recorder.FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("The raw bytes that were written are kept; session metadata is incomplete.", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"ERROR: capture aborted: {exc}", file=sys.stderr)
        try:
            session.finalize(recorder.STATUS_ERROR, str(exc), "io_error")
        except recorder.FinalizeError as finalize_exc:
            print(f"ERROR: {finalize_exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        # The port is closed on every path out of the capture.
        try:
            handle.close()
        except OSError as exc:
            print(f"warning: closing {args.port} failed: {exc}", file=sys.stderr)

    counters = info["counters"]
    print(
        f"status: {info['status']} | raw {counters['raw_bytes']} bytes in {counters['chunks']} chunks | "
        f"lines {counters['lines_total']} (CSI {counters['csi_lines']}, "
        f"parse errors {counters['csi_parse_error']}, oversized {counters['oversized_lines']}, "
        f"trailing partial {counters['trailing_partial_bytes']} bytes)"
    )
    if info["raw_sha256"] is None:
        print(f"raw sha256: unavailable ({info.get('raw_sha256_error', 'no raw file')})", file=sys.stderr)
    else:
        print(f"raw sha256: {info['raw_sha256']}")
    if info["error"]:
        print(f"error: {info['error']}", file=sys.stderr)
    if info["status"] == recorder.STATUS_COMPLETE:
        return EXIT_OK
    return EXIT_INTERRUPTED if info["status"] == recorder.STATUS_INTERRUPTED else EXIT_ERROR


def _fail(session: recorder.Session, message: str, error_type: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    try:
        session.fail_before_capture(message, error_type)
    except recorder.FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
    return EXIT_ERROR
