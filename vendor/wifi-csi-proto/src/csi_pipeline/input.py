"""Stage 1: decode stored ESP32-C5 captures without signal conditioning.

The capture writer stores int8 [imaginary, real] pairs, keeps one packet length
in csi_raw.npy, but writes every packet's metadata. Keep raw buffer positions:
physical subcarrier frequencies are deliberately not inferred from zero bins.

Format reference (I/Q order and first_word_invalid):
https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray

INTEGER_COLUMNS = (
    "seq",
    "rssi",
    "noise_floor",
    "fft_gain",
    "agc_gain",
    "channel",
    "local_timestamp",
    "sig_len",
    "rx_format",
    "len",
    "first_word",
    "dropped",
)
REQUIRED_COLUMNS = (*INTEGER_COLUMNS, "compensate_gain")
HARDWARE_VALIDITY_COLUMN = "rx_channel_estimate_info_vld"
CAPTURE_BYTES = 234
CAPTURE_RX_FORMAT = 2


class InputValidationError(ValueError):
    """Capture files cannot be interpreted without guessing or losing alignment."""


@dataclass(frozen=True)
class SessionData:
    """Unconditioned, aligned arrays; numerical arrays are read-only.

    csi and valid_sample_mask have shape [packet, raw bin]. Masks describe
    observable input usability, not respiration or a hardware validity guarantee.
    Metadata includes the original local_timestamp in microseconds. time_s
    preserves its differences, starting at zero. No unwrapping or resampling.

    Bins with no nonzero, first-word-valid observation in this entire offline
    capture are masked, not deleted. A streaming adapter needs its own policy.
    """

    session_id: str
    path: Path
    raw_csi: NDArray[np.int8]
    csi: NDArray[np.complex64]
    time_s: NDArray[np.float64]
    bin_indices: NDArray[np.int64]
    metadata_row_indices: NDArray[np.int64]
    metadata: Mapping[str, NDArray]
    session_info: Mapping[str, Any]
    valid_packet_mask: NDArray[np.bool_]
    valid_bin_mask: NDArray[np.bool_]
    valid_sample_mask: NDArray[np.bool_]


def _read_metadata(path: Path) -> dict[str, NDArray]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        names = reader.fieldnames
        if not names or len(names) != len(set(names)):
            raise ValueError("meta.csv: missing or duplicate column names")
        missing = set(REQUIRED_COLUMNS) - set(names)
        if missing:
            raise ValueError(f"meta.csv: missing columns {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("meta.csv: no packet rows")
    for index, row in enumerate(rows):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"meta.csv: malformed row at data index {index}")
    columns = {}
    for name in names:
        dtype = (
            np.int64
            if name in (*INTEGER_COLUMNS, HARDWARE_VALIDITY_COLUMN)
            else (np.float64 if name == "compensate_gain" else str)
        )
        try:
            columns[name] = np.asarray([row[name] for row in rows], dtype=dtype)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"meta.csv: invalid values in {name}") from exc
    if np.any(columns["len"] <= 0):
        raise ValueError("meta.csv: len must be positive")
    if not np.all(np.isin(columns["first_word"], [0, 1])):
        raise ValueError("meta.csv: first_word must be 0 or 1")
    if HARDWARE_VALIDITY_COLUMN in columns:
        if not np.all(np.isin(columns[HARDWARE_VALIDITY_COLUMN], [0, 1])):
            raise ValueError(f"meta.csv: {HARDWARE_VALIDITY_COLUMN} must be 0 or 1")

    gain = columns["compensate_gain"]

    if np.any(~np.isfinite(gain)) or np.any(gain <= 0):
        raise ValueError("meta.csv: compensate_gain must be finite and positive")

    timestamp_us = columns["local_timestamp"]

    if np.any(timestamp_us < 0) or np.any(timestamp_us > 2**32 - 1):
        raise ValueError("meta.csv: local_timestamp must be uint32 microseconds")

    return columns


def load_session(path: str | Path) -> SessionData:
    """Read one capture directory or raise InputValidationError.

    Supported profile: the repository's 234-byte, rx_format=2 captures.
    Reject ambiguous timestamps, unsupported profiles and inconsistent manifests.
    Gain coefficients and unusual seq/dropped counters are retained unchanged.
    The reader never loads respiration labels or derives them from folder names.
    """
    path = Path(path).resolve()

    try:
        return _load_session(path)
    except (OSError, ValueError, OverflowError, EOFError, csv.Error) as exc:
        raise InputValidationError(f"{path.name}: {exc}") from exc


def _load_session(path: Path) -> SessionData:
    raw = np.load(path / "csi_raw.npy", allow_pickle=False)

    if not isinstance(raw, np.ndarray):
        raw.close()
        raise ValueError("csi_raw.npy must contain one NumPy array")
    if raw.dtype != np.int8 or raw.ndim != 2:
        raise ValueError("csi_raw.npy must be a two-dimensional int8 array")
    n_packets, n_bytes = raw.shape
    if n_packets < 2:
        raise ValueError("at least two CSI packets are required")
    if n_bytes != CAPTURE_BYTES:
        raise ValueError(f"unsupported CSI width {n_bytes}; expected {CAPTURE_BYTES}")

    with (path / "session.json").open(encoding="utf-8-sig") as handle:
        info = json.load(handle)
    if not isinstance(info, dict):
        raise ValueError("session.json must contain an object")

    all_metadata = _read_metadata(path / "meta.csv")

    n_metadata = len(all_metadata["len"])

    expected = {
        "n_records": n_metadata,
        "n_kept": n_packets,
        "csi_bytes": n_bytes,
        "n_bins": n_bytes // 2,
    }

    for key, value in expected.items():
        if type(info.get(key)) is not int or info[key] != value:
            raise ValueError(f"session.json: {key} must equal {value}")

    # This reproduces the capture writer's keep-by-length rule, not a heuristic
    # truncation of metadata. Preserve the source row mapping for later audits.
    row_indices = np.flatnonzero(all_metadata["len"] == n_bytes)
    if row_indices.size != n_packets:
        raise ValueError(
            f"row alignment: {row_indices.size} metadata rows match CSI width, "
            f"but raw array has {n_packets} rows"
        )

    metadata = {name: values[row_indices] for name, values in all_metadata.items()}

    if np.any(metadata["rx_format"] != CAPTURE_RX_FORMAT):
        raise ValueError("unsupported rx_format; expected 2 for the capture profile")
    if np.unique(metadata["channel"]).size != 1:
        raise ValueError("channel changes inside capture; split before analysis")

    timestamp_us = metadata["local_timestamp"]
    interval_us = np.diff(timestamp_us)
    invalid = np.flatnonzero(interval_us <= 0)

    if invalid.size:
        row = int(invalid[0] + 1)
        raise ValueError(
            f"timestamps must be strictly increasing; aligned row {row} "
            f"(metadata data index {row_indices[row]}). "
            "Duplicate, reset, out-of-order and uint32-wrap cases need an "
            "explicit time-recovery policy; no automatic unwrap was applied"
        )

    time_s = (timestamp_us - timestamp_us[0]).astype(np.float64) / 1_000_000.0

    # Convert before arithmetic to avoid int8 overflow; do not apply gain here.
    csi = raw[:, 1::2].astype(np.complex64)
    csi.imag = raw[:, 0::2]
    sample_mask = np.ones(csi.shape, dtype=bool)
    sample_mask[metadata["first_word"].astype(bool), :2] = False

    if HARDWARE_VALIDITY_COLUMN in metadata:
        sample_mask[metadata[HARDWARE_VALIDITY_COLUMN] == 0] = False

    bin_mask = np.any(sample_mask & (csi != 0), axis=0)
    sample_mask &= bin_mask[None, :]
    packet_mask = np.any(sample_mask & (csi != 0), axis=1)
    sample_mask &= packet_mask[:, None]
    bin_indices = np.arange(csi.shape[1], dtype=np.int64)

    for array in (
        raw,
        csi,
        time_s,
        bin_indices,
        row_indices,
        sample_mask,
        bin_mask,
        packet_mask,
        *metadata.values(),
    ):
        array.setflags(write=False)

    return SessionData(
        session_id=path.name,
        path=path,
        raw_csi=raw,
        csi=csi,
        time_s=time_s,
        bin_indices=bin_indices,
        metadata_row_indices=row_indices,
        metadata=MappingProxyType(metadata),
        session_info=MappingProxyType(info),
        valid_packet_mask=packet_mask,
        valid_bin_mask=bin_mask,
        valid_sample_mask=sample_mask,
    )


def summarize_session(session: SessionData) -> dict[str, Any]:
    """JSON-compatible input audit, without quality thresholds or respiration."""
    interval_s = np.diff(session.time_s)
    duration_s = float(session.time_s[-1])
    n_packets = len(session.time_s)
    raw_zero_bins = np.flatnonzero(np.all(session.csi == 0, axis=0)).tolist()

    return {
        "session_id": session.session_id,
        "n_packets": n_packets,
        "n_metadata_rows": session.session_info["n_records"],
        "n_omitted_metadata_rows": session.session_info["n_records"] - n_packets,
        "n_bins": session.csi.shape[1],
        "n_valid_bins": int(session.valid_bin_mask.sum()),
        "invalid_bin_indices": session.bin_indices[~session.valid_bin_mask].tolist(),
        "all_zero_bin_indices": raw_zero_bins,
        "n_invalid_packets": int((~session.valid_packet_mask).sum()),
        "n_first_word_invalid_packets": int(session.metadata["first_word"].sum()),
        "duration_s": duration_s,
        "median_interval_s": float(np.median(interval_s)),
        "min_interval_s": float(interval_s.min()),
        "max_interval_s": float(interval_s.max()),
        "sample_rate_hz_from_median": float(1.0 / np.median(interval_s)),
        "sample_rate_hz_from_span": (n_packets - 1) / duration_s,
        "channel": int(session.metadata["channel"][0]),
        "rx_format": int(session.metadata["rx_format"][0]),
        "gain_applied": False,
        "resampled": False,
        "bin_index_basis": "raw_buffer_position",
        "hardware_validity_flag_recorded": (
            HARDWARE_VALIDITY_COLUMN in session.metadata
        ),
    }
