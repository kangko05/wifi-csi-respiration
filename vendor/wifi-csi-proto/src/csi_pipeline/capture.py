"""Lossless serial acquisition for the existing ESP32-C5 base64 CSI format."""

from __future__ import annotations

import base64
import binascii
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .input import CAPTURE_BYTES, InputValidationError, load_session


WIRE_COLUMNS = (
    "seq", "rssi", "noise_floor", "fft_gain", "agc_gain", "channel",
    "local_timestamp", "sig_len", "rx_format", "len", "first_word",
    "compensate_gain", "dropped",
)


class SerialStream(Protocol):
    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int) -> bytes: ...


@dataclass(frozen=True)
class CaptureConfig:
    duration_s: float = 120.0
    idle_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("duration_s", "idle_timeout_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    status: str
    n_kept: int
    input_valid: bool
    error: str | None


def parse_csi_line(line: bytes) -> tuple[dict[str, int | float], bytes] | None:
    """Ignore firmware logs; reject malformed CSI without changing its bytes."""
    if not line.startswith(b"CSI_DATA,"):
        return None
    try:
        fields = line.strip().decode("ascii").split(",")
        if len(fields) != len(WIRE_COLUMNS) + 2:
            raise ValueError("unexpected CSI field count")
        metadata = {
            name: float(value) if name == "compensate_gain" else int(value)
            for name, value in zip(WIRE_COLUMNS, fields[1:-1], strict=True)
        }
        if not math.isfinite(metadata["compensate_gain"]):
            raise ValueError("nonfinite compensate_gain")
        payload = base64.b64decode(fields[-1], validate=True)
        if len(payload) != metadata["len"] or not payload:
            raise ValueError("CSI payload length mismatch")
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"invalid CSI_DATA: {exc}") from exc
    return metadata, payload


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def capture_session(
    stream: SerialStream,
    path: Path,
    config: CaptureConfig,
    context: dict,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    notify: Callable[[str], None] = print,
) -> CaptureResult:
    """Save raw serial bytes continuously; finalize available packets on interruption.

    The caller opens the port, completes the countdown, and clears old serial data.
    Metadata retains all decoded lengths; the array keeps the supported 234 bytes.
    Timestamps, I/Q order, first-word flags and gain values are never repaired.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    info = dict(
        capture_schema=1, status="recording", context=context,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
        requested_duration_s=config.duration_s, idle_timeout_s=config.idle_timeout_s,
        csi_bytes=CAPTURE_BYTES, n_bins=CAPTURE_BYTES // 2,
        n_records=0, n_kept=0, other_lines=0, malformed_lines=0,
        unsupported_length_records=0, oversized_lines=0,
        labels_used_in_estimation=False,
    )
    _write_json(path / "session.json", info)
    payloads: list[bytes] = []
    pending = bytearray()
    discarding = False
    start = last_packet = last_progress = monotonic()
    status, error = "complete", None
    notify("START: count breaths now. / 지금부터 호흡 횟수를 세세요.")
    with (path / "serial.log").open("xb") as log, (path / "meta.csv").open("x", encoding="utf-8", newline="") as meta:
        writer = csv.DictWriter(meta, fieldnames=WIRE_COLUMNS)
        writer.writeheader()
        try:
            while monotonic() - start < config.duration_s:
                chunk = stream.read(min(65536, max(1, stream.in_waiting)))
                log.write(chunk)
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if discarding or len(line) > 65536:
                        if not discarding:
                            info["oversized_lines"] += 1
                        discarding = False
                        continue
                    try:
                        packet = parse_csi_line(bytes(line))
                    except ValueError:
                        info["malformed_lines"] += 1
                        continue
                    if packet is None:
                        info["other_lines"] += 1
                        continue
                    metadata, payload = packet
                    writer.writerow(metadata)
                    info["n_records"] += 1
                    if len(payload) == CAPTURE_BYTES:
                        payloads.append(payload)
                        last_packet = monotonic()
                    else:
                        info["unsupported_length_records"] += 1
                if len(pending) > 65536:
                    if not discarding:
                        info["oversized_lines"] += 1
                    pending.clear()
                    discarding = True
                now = monotonic()
                # Persist transport bytes/metadata even if the process is later killed.
                log.flush()
                meta.flush()
                if now - last_packet >= config.idle_timeout_s:
                    raise TimeoutError(f"No 234-byte CSI packet for {config.idle_timeout_s:g}s; check RX port, TX and firmware.")
                if now - last_progress >= 10:
                    notify(f"{now - start:.0f}/{config.duration_s:g}s | CSI {len(payloads)} packets")
                    last_progress = now
        except KeyboardInterrupt:
            status, error = "interrupted", "Stopped with Ctrl+C; partial capture retained."
        except (OSError, TimeoutError) as exc:
            status, error = "error", str(exc)
        finally:
            elapsed = monotonic() - start
            notify("STOP: stop counting. / 호흡 횟수 세기를 마치세요.")
            raw = np.frombuffer(b"".join(payloads), dtype=np.int8).reshape(-1, CAPTURE_BYTES)
            with (path / "csi_raw.npy").open("xb") as handle:
                np.save(handle, raw, allow_pickle=False)
            info.update(
                status=status, error=error, n_kept=len(payloads),
                elapsed_host_s=elapsed, ended_at_utc=datetime.now(timezone.utc).isoformat(),
                trailing_partial_bytes=len(pending),
            )
            # Close metadata buffering before input validation reads it.
            meta.flush()
            _write_json(path / "session.json", info)
    try:
        session = load_session(path)
        info.update(input_valid=True, input_error=None,
                    csi_duration_s=float(session.time_s[-1] - session.time_s[0]))
    except InputValidationError as exc:
        info.update(input_valid=False, input_error=str(exc), csi_duration_s=None)
    _write_json(path / "session.json", info)
    return CaptureResult(path, status, len(payloads), info["input_valid"], error or info["input_error"])


def save_evaluation(path: Path, count: int | None, uncertain: bool, movement_note: str) -> None:
    """Keep manual labels separate; a host-cue count is not a synchronized target."""
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise ValueError("count must be a nonnegative integer or None")
    value = dict(
        manual_breath_count=count, count_uncertain=uncertain,
        movement_note=movement_note, reference_scope="host START/STOP cues",
        synchronized_to_csi=False, used_in_estimation=False,
        empty_room_label=None,
    )
    with (Path(path) / "evaluation.json").open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
