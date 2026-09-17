"""Raw serial session recorder.

Contract (see `docs/data-collection.md`):

* every received byte is appended to `serial.bin` unmodified, including boot
  logs, malformed lines and any trailing partial line;
* `chunks.csv` indexes each read with raw byte offset/length and host receipt
  time (monotonic elapsed + UTC), which stays distinct from the device's own
  `local_timestamp`;
* `lines.csv` indexes every physical line with raw offset/length, parse verdict
  and the original wire metadata strings; nothing is dropped, zero filled,
  interpolated, de-duplicated or time corrected;
* `session.json` records status/config/conditions/counters and the raw SHA256.

Adapted ideas (raw-log-first capture, base64 line parsing, idle timeout,
counter bookkeeping) from the read-only legacy reference
`../wifi-csi-proto/src/csi_pipeline/capture.py`
(HEAD 397813b960b8fd114ba5a78c367d5e26323613df,
SHA256 EED00D6A3EBFCF9B5008D0192A8D6EA70269E2F644AEDA456C7448A8661A68B1).
Changes: no fixed 234-byte numpy array, no payload list in memory, byte offsets
instead of duplicated payloads, all physical lines indexed, oversized/trailing
bytes accounted, no input-validation/label coupling.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import secrets
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import wire

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 65536
READ_CHUNK_BYTES = 65536
POLL_INTERVAL_S = 0.05
FLUSH_INTERVAL_S = 1.0
PROGRESS_INTERVAL_S = 10.0

CHUNK_COLUMNS = ("chunk_index", "byte_offset", "byte_length", "host_elapsed_s", "host_utc")
LINE_COLUMNS = (
    "line_index",
    "byte_offset",
    "byte_length",
    "terminator",
    "terminator_length",
    "kind",
    "parse_ok",
    "error",
    "arrival_chunk_index",
    "host_elapsed_s",
    "host_utc",
    "data_byte_offset",
    "data_byte_length",
    "decoded_length",
    "len_match",
) + wire.WIRE_FIELDS

STATUS_RECORDING = "recording"
STATUS_COMPLETE = "complete"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"


class SerialStream(Protocol):
    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int) -> bytes: ...


class IdleTimeout(Exception):
    """No bytes arrived within the bounded idle timeout."""


class FinalizeError(Exception):
    """Session metadata could not be written; the capture is not trustworthy."""


def require_positive_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive (got {value!r})")
    return value


def new_session_id(now_utc: datetime) -> str:
    """UTC time down to microseconds plus randomness, so IDs do not collide."""
    return f"{now_utc.strftime('%Y%m%dT%H%M%S_%f')}_{secrets.token_hex(4)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class _LineAssembler:
    """Splits the stream into physical lines while keeping raw byte offsets.

    Memory is bounded: a line longer than `max_line_bytes` stops being buffered
    and is reported as oversized, but its raw bytes stay in `serial.bin` and its
    offset/length accounting stays exact.
    """

    def __init__(self, start_offset: int = 0, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self.start_offset = start_offset
        self.max_line_bytes = max_line_bytes
        self._buf = bytearray()
        self._total = 0
        self._oversized = False
        self._last_byte = -1

    def feed(self, chunk: bytes, base_offset: int):
        """Yield (byte_offset, byte_length, content, terminator, oversized) per line.

        `byte_length` and `byte_offset` describe the line content inside the raw
        stream; the terminator bytes are excluded but reported separately.
        """
        pos = 0
        while True:
            nl = chunk.find(b"\n", pos)
            if nl < 0:
                self._absorb(chunk[pos:])
                return
            self._absorb(chunk[pos:nl])
            total = base_offset + nl - self.start_offset
            crlf = self._last_byte == 0x0D
            content = b""
            if not self._oversized:
                content = bytes(self._buf[:-1]) if crlf else bytes(self._buf)
            yield (
                self.start_offset,
                total - 1 if crlf else total,
                content,
                "crlf" if crlf else "lf",
                self._oversized,
            )
            self.start_offset = base_offset + nl + 1
            self._buf = bytearray()
            self._total = 0
            self._oversized = False
            self._last_byte = -1
            pos = nl + 1

    def _absorb(self, segment: bytes) -> None:
        if not segment:
            return
        self._total += len(segment)
        self._last_byte = segment[-1]
        if self._oversized:
            return
        self._buf.extend(segment)
        if len(self._buf) > self.max_line_bytes:
            self._oversized = True
            self._buf = bytearray()

    @property
    def pending_bytes(self) -> int:
        return self._total

    @property
    def pending_oversized(self) -> bool:
        return self._oversized


def create_session(
    data_root: Path,
    *,
    config: dict,
    conditions: dict,
    host: dict,
    utcnow: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    notify: Callable[[str], None] = print,
) -> "Session":
    """Create a fresh session directory and write the `recording` status first.

    `mkdir(exist_ok=False)` refuses to reuse an existing directory, so no earlier
    session's bytes can be overwritten.
    """
    now = utcnow()
    path = Path(data_root) / new_session_id(now)
    path.mkdir(parents=True, exist_ok=False)
    info = {
        "schema": SCHEMA_VERSION,
        "session_id": path.name,
        "status": STATUS_RECORDING,
        "status_note": (
            "Written before the capture attempt. A session left at 'recording' "
            "means the process was killed before finalizing; treat its files as "
            "a partial, unfinalized capture."
        ),
        "created_at_utc": now.isoformat(),
        "started_at_utc": None,
        "ended_at_utc": None,
        "config": dict(config),
        "conditions": dict(conditions),
        "unknown_conditions": sorted(k for k, v in conditions.items() if v is None),
        "conditions_note": (
            "null means unknown/not provided (an empty string means explicitly "
            "empty). Conditions are operator-entered context only and are never "
            "used as ground truth by any processing."
        ),
        "host": dict(host),
        "verification": {
            "hardware_identity_verified": False,
            "firmware_version_known": False,
            "tx_power_verified": False,
            "note": (
                "The collector records only what the host received. Board "
                "identity, running firmware version and RF output are not "
                "verified here."
            ),
        },
        "interpretation": {
            "byte_order_preserved": True,
            "iq_decoded": False,
            "subcarrier_selection": False,
            "signed_interpretation": "deferred",
            "note": (
                "Base64 payload bytes are stored in serial.bin in the received "
                "order. int8/IQ interpretation is deferred to later analysis."
            ),
        },
        "labels": {
            "manual_reference_recorded": False,
            "note": "Manual breath counts/labels belong to a separate evaluation record made after capture.",
        },
        "counters": {},
        "payload_length_counts": {},
        "timing": {},
        "raw_sha256": None,
        "error": None,
        "error_type": None,
        "raw_immutable_after_close": True,
        "files": {
            "raw": "serial.bin",
            "chunks": "chunks.csv",
            "lines": "lines.csv",
            "session": "session.json",
        },
    }
    _write_json(path / "session.json", info)
    return Session(path, info, monotonic=monotonic, utcnow=utcnow, notify=notify)


class Session:
    def __init__(
        self,
        path: Path,
        info: dict,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = _utcnow,
        notify: Callable[[str], None] = print,
    ) -> None:
        self.path = Path(path)
        self.info = info
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._notify = notify

    # -- finalization ----------------------------------------------------
    def finalize(self, status: str, error: str | None = None, error_type: str | None = None) -> dict:
        """Write the final status. Never claims success if metadata cannot be saved."""
        self.info["status"] = status
        self.info["error"] = error
        self.info["error_type"] = error_type
        self.info["ended_at_utc"] = self._utcnow().isoformat()
        raw_path = self.path / "serial.bin"
        if raw_path.exists():
            try:
                digest = hashlib.sha256()
                with raw_path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(block)
                self.info["raw_sha256"] = digest.hexdigest()
                self.info["raw_bytes_on_disk"] = raw_path.stat().st_size
            except OSError as exc:  # the bytes stay on disk; we just cannot verify them
                self.info["raw_sha256"] = None
                self.info["raw_sha256_error"] = str(exc)
                if status == STATUS_COMPLETE:
                    status = STATUS_ERROR
                    self.info["error"] = f"could not hash serial.bin: {exc}"
                    self.info["error_type"] = "io_error"
                self.info["status"] = status
        try:
            _write_json(self.path / "session.json", self.info)
        except OSError as exc:
            raise FinalizeError(f"could not write session.json in {self.path}: {exc}") from exc
        return self.info

    def fail_before_capture(self, error: str, error_type: str) -> dict:
        """Mark a failed capture (e.g. the port could never be opened)."""
        self.info["started_at_utc"] = None
        self.info["counters"] = _empty_counters()
        return self.finalize(STATUS_ERROR, error, error_type)

    # -- capture ---------------------------------------------------------
    def record(self, stream: SerialStream, duration_s: float, idle_timeout_s: float) -> dict:
        duration_s = require_positive_finite("duration_s", duration_s)
        idle_timeout_s = require_positive_finite("idle_timeout_s", idle_timeout_s)

        counters = _empty_counters()
        lengths: Counter[int] = Counter()
        assembler = _LineAssembler()
        raw_offset = 0
        chunk_index = 0
        line_index = 0
        first_byte_elapsed: float | None = None
        last_byte_elapsed: float | None = None
        # Receipt time of the last chunk that actually carried bytes; the trailing
        # partial line is timed by this, not by the moment the capture ended.
        last_chunk_elapsed: float | None = None
        last_chunk_utc: str | None = None
        status, error, error_type = STATUS_COMPLETE, None, None
        elapsed = 0.0

        start_utc = self._utcnow()
        self.info["started_at_utc"] = start_utc.isoformat()

        # Any file-level IO failure (creating, writing, flushing or closing the
        # session files) ends as a reported error; it never claims completion.
        try:
            _write_json(self.path / "session.json", self.info)
            self._notify(
                f"START {start_utc.isoformat()} (host local {datetime.now().isoformat(timespec='seconds')}) "
                f"| duration {duration_s:g}s | session {self.path.name}"
            )

            with (self.path / "serial.bin").open("xb") as raw, \
                    (self.path / "chunks.csv").open("x", encoding="utf-8", newline="") as chunk_file, \
                    (self.path / "lines.csv").open("x", encoding="utf-8", newline="") as line_file:
                chunk_writer = csv.writer(chunk_file)
                chunk_writer.writerow(CHUNK_COLUMNS)
                line_writer = csv.DictWriter(line_file, fieldnames=LINE_COLUMNS, extrasaction="ignore")
                line_writer.writeheader()
                chunk_file.flush()
                line_file.flush()

                start = self._monotonic()
                last_data = start
                last_flush = start
                last_progress = start
                try:
                    while True:
                        now = self._monotonic()
                        elapsed = now - start
                        if elapsed >= duration_s:
                            break
                        try:
                            chunk = self._read(stream)
                        except OSError as exc:  # only the serial side is a serial error
                            status, error, error_type = STATUS_ERROR, f"serial/IO error: {exc}", "serial_error"
                            break
                        now = self._monotonic()
                        elapsed = now - start
                        if chunk:
                            arrival_utc = self._utcnow().isoformat()
                            raw.write(chunk)
                            chunk_writer.writerow((chunk_index, raw_offset, len(chunk), f"{elapsed:.6f}", arrival_utc))
                            if first_byte_elapsed is None:
                                first_byte_elapsed = elapsed
                            last_byte_elapsed = elapsed
                            last_chunk_elapsed, last_chunk_utc = elapsed, arrival_utc
                            counters["chunks"] += 1
                            counters["raw_bytes"] += len(chunk)
                            for offset, length, content, terminator, oversized in assembler.feed(chunk, raw_offset):
                                row = _line_row(
                                    line_index, offset, length, terminator, oversized, content,
                                    chunk_index, elapsed, arrival_utc, counters, lengths,
                                )
                                line_writer.writerow(row)
                                line_index += 1
                            raw_offset += len(chunk)
                            chunk_index += 1
                            last_data = now
                        elif now - last_data >= idle_timeout_s:
                            raise IdleTimeout(
                                f"no serial bytes for {idle_timeout_s:g}s "
                                f"(checked at {elapsed:.1f}s of {duration_s:g}s)"
                            )
                        if now - last_flush >= FLUSH_INTERVAL_S:
                            raw.flush()
                            chunk_file.flush()
                            line_file.flush()
                            last_flush = now
                        if now - last_progress >= PROGRESS_INTERVAL_S:
                            self._notify(
                                f"{elapsed:.0f}/{duration_s:g}s | {counters['raw_bytes']} raw bytes | "
                                f"{counters['csi_lines']} CSI lines ({counters['csi_parse_error']} with parse errors)"
                            )
                            last_progress = now
                except KeyboardInterrupt:
                    status = STATUS_INTERRUPTED
                    error = "stopped with Ctrl+C; partial session retained"
                    error_type = "keyboard_interrupt"
                except IdleTimeout as exc:
                    status, error, error_type = STATUS_ERROR, str(exc), "idle_timeout"
                finally:
                    elapsed = self._monotonic() - start
                    try:
                        self._close_out(
                            assembler, line_writer, line_index, chunk_index,
                            last_chunk_elapsed, last_chunk_utc, counters, raw, chunk_file, line_file,
                        )
                    except OSError as exc:  # disk full or similar: report, never claim success
                        if status == STATUS_COMPLETE:
                            status = STATUS_ERROR
                            error = f"could not finish writing session files: {exc}"
                            error_type = "io_error"
        except OSError as exc:
            # Raised while creating the files, while writing the raw stream, or
            # while closing/flushing them at the end of the `with` block.
            if status == STATUS_COMPLETE:
                status, error, error_type = STATUS_ERROR, f"session file IO failed: {exc}", "io_error"

        stop_utc = self._utcnow()
        self._notify(f"STOP  {stop_utc.isoformat()} (host local {datetime.now().isoformat(timespec='seconds')})")
        self.info["counters"] = counters
        self.info["payload_length_counts"] = {str(k): v for k, v in sorted(lengths.items())}
        self.info["timing"] = {
            "elapsed_host_s": round(elapsed, 6),
            "first_byte_elapsed_s": None if first_byte_elapsed is None else round(first_byte_elapsed, 6),
            "last_byte_elapsed_s": None if last_byte_elapsed is None else round(last_byte_elapsed, 6),
            "note": "Host receipt times only; device local_timestamp values are kept per line in lines.csv.",
        }
        return self.finalize(status, error, error_type)

    def _close_out(
        self, assembler, line_writer, line_index, chunk_index,
        last_chunk_elapsed, last_chunk_utc, counters, raw, chunk_file, line_file,
    ) -> None:
        """Record the trailing partial line (if any) and flush every open file.

        The trailing row is timed by the chunk it arrived in (`arrival_chunk_index`),
        not by the later moment the capture stopped; the capture end time is kept
        separately in `session.json`'s `timing`.
        """
        if assembler.pending_bytes:
            # Trailing partial line: retained raw, never completed or repaired.
            counters["trailing_partial_bytes"] = assembler.pending_bytes
            counters["lines_total"] += 1
            line_writer.writerow({
                "line_index": line_index,
                "byte_offset": assembler.start_offset,
                "byte_length": assembler.pending_bytes,
                "terminator": "none",
                "terminator_length": 0,
                "kind": "trailing_partial",
                "parse_ok": 0,
                "error": "no_terminator_at_end_of_capture",
                "arrival_chunk_index": max(chunk_index - 1, -1),
                "host_elapsed_s": "" if last_chunk_elapsed is None else f"{last_chunk_elapsed:.6f}",
                "host_utc": last_chunk_utc or "",
                "data_byte_offset": -1,
                "data_byte_length": -1,
                "decoded_length": -1,
                "len_match": "",
            })
        raw.flush()
        chunk_file.flush()
        line_file.flush()

    def _read(self, stream: SerialStream) -> bytes:
        waiting = stream.in_waiting
        if waiting:
            return stream.read(min(waiting, READ_CHUNK_BYTES))
        return stream.read(1)


def _empty_counters() -> dict:
    return {
        "chunks": 0,
        "raw_bytes": 0,
        "lines_total": 0,
        "csi_lines": 0,
        "csi_parse_ok": 0,
        "csi_parse_error": 0,
        "len_mismatch_lines": 0,
        "base64_error_lines": 0,
        "first_word_invalid_lines": 0,
        "header_lines": 0,
        "other_lines": 0,
        "oversized_lines": 0,
        "trailing_partial_bytes": 0,
    }


def _line_row(
    line_index: int,
    offset: int,
    length: int,
    terminator: str,
    oversized: bool,
    content: bytes,
    chunk_index: int,
    elapsed: float,
    arrival_utc: str,
    counters: dict,
    lengths: Counter,
) -> dict:
    counters["lines_total"] += 1
    row = {
        "line_index": line_index,
        "byte_offset": offset,
        "byte_length": length,
        "terminator": terminator,
        "terminator_length": 2 if terminator == "crlf" else 1,
        "arrival_chunk_index": chunk_index,
        "host_elapsed_s": f"{elapsed:.6f}",
        "host_utc": arrival_utc,
        "data_byte_offset": -1,
        "data_byte_length": -1,
        "decoded_length": -1,
        "len_match": "",
    }
    if oversized:
        counters["oversized_lines"] += 1
        row.update(kind="oversized", parse_ok=0, error=f"line_exceeds_{MAX_LINE_BYTES}_bytes")
        return row

    parsed = wire.parse_line(content)
    row.update(kind=parsed.kind, parse_ok=int(parsed.parse_ok), error=parsed.error)
    row.update(parsed.fields)
    if parsed.kind == wire.KIND_CSI:
        counters["csi_lines"] += 1
        counters["csi_parse_ok" if parsed.parse_ok else "csi_parse_error"] += 1
        if "base64_error" in parsed.error:
            counters["base64_error_lines"] += 1
        if "len_mismatch" in parsed.error:
            counters["len_mismatch_lines"] += 1
        if parsed.fields.get("first_word") not in (None, "0"):
            counters["first_word_invalid_lines"] += 1
        if parsed.data_rel_offset >= 0:
            row["data_byte_offset"] = offset + parsed.data_rel_offset
            row["data_byte_length"] = parsed.data_length
        row["decoded_length"] = parsed.decoded_length
        row["len_match"] = parsed.len_match
        if parsed.decoded_length >= 0:
            lengths[parsed.decoded_length] += 1
    elif parsed.kind == wire.KIND_HEADER:
        counters["header_lines"] += 1
    else:
        counters["other_lines"] += 1
    return row
