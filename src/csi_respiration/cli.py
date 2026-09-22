"""Command line wrapper: one raw CSI_DATA file (or stdin) -> one pipeline window -> JSON.

    .venv/bin/python main.py --input capture.csv [--methods amplitude phase cir] [--output result.json]
    PYTHONPATH=src .venv/bin/python -m csi_respiration --input - < capture.csv

The whole input, read to EOF, is one `RespirationPipeline.process` call. Nothing
is streamed, scheduled, cropped or repaired. Physical lines are split on LF and
one CR directly before that LF is removed; every other byte reaches
`preprocessing.to_CSIdata` unchanged. Empty physical lines are skipped and counted.
Any line containing `CSI_DATA` is a record and must validate. Other text is an
error unless `--serial-log` is given, in which case it is skipped and counted.
See `docs/python-cli.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .adapter import PipelineInputError
from .pipeline import METHODS, PipelineConfig, RespirationPipeline

SCHEMA = "csi_respiration.cli/1"
STDIN = "-"
CSI_MARKER = b"CSI_DATA"

EXIT_OK = 0
EXIT_USAGE = 2  # argparse errors and invalid method/session settings
EXIT_INPUT = 3  # input bytes are not an analysable CSI_DATA window
EXIT_IO = 4  # input unreadable, output exists/unwritable

_INDEXED = re.compile(r"^(record|packet) (\d+): ")

EPILOG = """\
examples:
  .venv/bin/python main.py --input capture.csv
  .venv/bin/python main.py -i capture.csv --methods amplitude -o result.json
  .venv/bin/python main.py -i boot_and_csi.txt --serial-log --session-id run1
  PYTHONPATH=src .venv/bin/python -m csi_respiration -i - < capture.csv

The input is read to EOF and analysed as one window; stdin is not a live serial
stream. JSON goes to stdout (or only to --output); diagnostics go to stderr.
exit status: 0 processed (any method status, including withheld/insufficient_data),
2 usage/configuration, 3 input data error, 4 file I/O error or output refused.
"""


class CliError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class InputLines:
    """Records to analyse plus an account of every physical line that was not one."""

    records: list[bytes] = field(default_factory=list)
    record_lines: list[int] = field(default_factory=list)  # 1-based physical line per record
    empty_lines: list[int] = field(default_factory=list)
    skipped_lines: list[int] = field(default_factory=list)  # non-CSI text, --serial-log only
    n_physical_lines: int = 0
    n_crlf: int = 0
    n_lf: int = 0
    unterminated_last_line: bool = False


def _preview(line: bytes, limit: int = 60) -> str:
    text = repr(line[:limit])
    return text + ("..." if len(line) > limit else "")


def split_input(data: bytes, *, serial_log: bool, source: str) -> InputLines:
    """Split raw bytes into physical lines and classify them; raises CliError(EXIT_INPUT)."""

    out = InputLines()
    pieces = data.split(b"\n")
    last_terminated = pieces[-1] == b""
    if last_terminated:
        pieces.pop()
    else:
        out.unterminated_last_line = True

    for number, line in enumerate(pieces, start=1):
        terminated = number < len(pieces) or last_terminated
        if terminated:
            if line.endswith(b"\r"):
                line = line[:-1]
                out.n_crlf += 1
            else:
                out.n_lf += 1

        if not line:
            out.empty_lines.append(number)
        elif CSI_MARKER in line:
            out.records.append(line)
            out.record_lines.append(number)
        elif serial_log:
            out.skipped_lines.append(number)
        else:
            raise CliError(
                EXIT_INPUT,
                f"{source}:{number}: not a CSI_DATA line: {_preview(line)}; the default input is raw "
                "CSI_DATA lines only (use --serial-log to skip boot/log/header text)",
            )

    out.n_physical_lines = len(pieces)

    if not out.records:
        raise CliError(
            EXIT_INPUT,
            f"{source}: no CSI_DATA lines ({out.n_physical_lines} physical lines, "
            f"{len(out.empty_lines)} empty, {len(out.skipped_lines)} skipped non-CSI)",
        )

    return out


def _located(exc: PipelineInputError, lines: InputLines, source: str) -> str:
    """Prefix a pipeline error with the physical line of the record/packet it names."""

    message = str(exc)
    match = _INDEXED.match(message)

    if match:
        index = int(match.group(2))
        if 0 <= index < len(lines.record_lines):
            return f"{source}:{lines.record_lines[index]}: {message}"

    return f"{source}: {message}"


def _read_input(path: str, stdin) -> bytes:
    try:
        if path == STDIN:
            return stdin.read()

        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise CliError(EXIT_IO, f"cannot read input {path!r}: {exc.strerror or exc}") from exc


def _check_output(path: str) -> None:
    """Fail before the (possibly slow) analysis; the write itself is O_EXCL anyway."""

    if path == STDIN:
        raise CliError(EXIT_USAGE, "--output - is not supported; omit --output to write JSON to stdout")

    if os.path.lexists(path):
        raise CliError(EXIT_IO, f"refusing to overwrite existing output {path!r}")

    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        raise CliError(EXIT_IO, f"output directory {parent!r} does not exist (it is not created)")


def _write_new(path: str, text: str) -> None:
    data = text.encode("utf-8")

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise CliError(EXIT_IO, f"refusing to overwrite existing output {path!r}") from exc
    except OSError as exc:
        raise CliError(EXIT_IO, f"cannot create output {path!r}: {exc.strerror or exc}") from exc

    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        os.unlink(path)  # created exclusively above, so the partial file is ours
        raise CliError(EXIT_IO, f"cannot write output {path!r}: {exc.strerror or exc}") from exc


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Analyse one complete file of raw ESP32 CSI_DATA lines as a single respiration\n"
            "window with the unchanged amplitude/phase/CIR pipeline and print JSON."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-i", "--input", required=True, metavar="PATH",
        help="file of raw CSI_DATA lines (LF or CRLF), or '-' for stdin read to EOF",
    )

    parser.add_argument(
        "--methods", nargs="+", choices=METHODS, default=list(METHODS), metavar="METHOD",
        help=f"methods to run, in output order (choices: {', '.join(METHODS)}; default: all)",
    )

    parser.add_argument(
        "-o", "--output", metavar="PATH",
        help="write JSON to this NEW file instead of stdout; never overwrites, parent must exist",
    )

    parser.add_argument(
        "--session-id", metavar="ID",
        help="identifier stored in the result (default: input file name, or 'stdin')",
    )

    parser.add_argument(
        "--serial-log", action="store_true",
        help="skip and count non-CSI text lines (boot log, header); lines containing "
        "CSI_DATA must still be valid records",
    )

    return parser


def run(args: argparse.Namespace, stdin, stdout, stderr) -> int:
    source = "<stdin>" if args.input == STDIN else args.input

    if args.output is not None:
        _check_output(args.output)

    session_id = args.session_id
    if session_id is None:
        session_id = "stdin" if args.input == STDIN else Path(args.input).name

    if not session_id:
        raise CliError(EXIT_USAGE, "--session-id must not be empty")

    try:
        config = PipelineConfig(methods=tuple(args.methods), session_id=session_id)
    except (TypeError, ValueError) as exc:
        raise CliError(EXIT_USAGE, f"invalid configuration: {exc}") from exc

    data = _read_input(args.input, stdin)
    lines = split_input(data, serial_log=args.serial_log, source=source)

    try:
        result = RespirationPipeline(config).process(lines.records)
    except PipelineInputError as exc:
        raise CliError(EXIT_INPUT, _located(exc, lines, source)) from exc

    document = {
        "schema": SCHEMA,
        "input": {
            "source": args.input,
            "mode": "serial_log" if args.serial_log else "strict",
            "n_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "n_physical_lines": lines.n_physical_lines,
            "n_crlf_lines": lines.n_crlf,
            "n_lf_lines": lines.n_lf,
            "unterminated_last_line": lines.unterminated_last_line,
            "n_records": len(lines.records),
            "first_record_line": lines.record_lines[0],
            "last_record_line": lines.record_lines[-1],
            "n_empty_lines": len(lines.empty_lines),
            "empty_line_numbers": lines.empty_lines,
            "n_skipped_non_csi_lines": len(lines.skipped_lines),
            "skipped_non_csi_line_numbers": lines.skipped_lines,
        },
        "result": result.to_dict(),
    }
    text = json.dumps(document, allow_nan=False, ensure_ascii=False, indent=2) + "\n"

    if args.output is not None:
        _write_new(args.output, text)
    else:
        stdout.write(text)
        stdout.flush()

    statuses = ", ".join(f"{name}={m.status}" for name, m in result.methods.items())
    print(
        f"{source}: {len(lines.records)} CSI_DATA records from {lines.n_physical_lines} lines "
        f"({len(lines.empty_lines)} empty, {len(lines.skipped_lines)} non-CSI skipped); {statuses}"
        + (f"; wrote {args.output}" if args.output is not None else ""),
        file=stderr,
    )

    return EXIT_OK


def main(argv: list[str] | None = None, *, prog: str | None = None,
         stdin=None, stdout=None, stderr=None) -> int:
    """Entry point; returns the exit status. `stdin` is a binary stream."""

    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    parser = build_parser(prog)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help (0) or a usage error (2); argparse already printed
        return int(exc.code or 0)

    try:
        return run(args, stdin, stdout, stderr)
    except CliError as exc:
        print(f"error: {exc}", file=stderr)
        return exc.code
