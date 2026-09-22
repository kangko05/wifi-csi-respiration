"""Records in, vendored `SessionData` out, entirely in memory.

Both accepted input forms end in the same check: raw lines go through
`preprocessing.to_CSIdata`, which ends in `validate_CSIdata`; ready-made
CSIdata records go through `validate_CSIdata` directly. `build_session` then
reproduces `csi_pipeline.input.load_session` for the one supported capture
profile without writing or reading any file.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np

from csi_adapt.legacy_format import ConversionError, resolve_timestamps
from preprocessing import CSIdata, to_CSIdata, validate_CSIdata

from ._legacy import CAPTURE_BYTES, CAPTURE_RX_FORMAT, SessionData

IN_MEMORY_PATH = Path("<in-memory>")

# Supported input profile, identical to the vendored loader's.
SUPPORTED_PROFILE = MappingProxyType({
    "csi_bytes": CAPTURE_BYTES,
    "n_bins": CAPTURE_BYTES // 2,
    "rx_format": CAPTURE_RX_FORMAT,
    "single_channel": True,
})

INPUT_RAW = "raw"
INPUT_CSIDATA = "csidata"


class PipelineInputError(ValueError):
    """The window cannot be analysed without guessing."""


class RecordValidationError(PipelineInputError):
    """One record is malformed; `index` is its position in the caller's input."""

    def __init__(self, index: int, message: str):
        super().__init__(f"record {index}: {message}")
        self.index = index


class UnsupportedProfileError(PipelineInputError):
    """Valid records outside the supported 234-byte/rx_format 2/single channel profile."""


class TimestampOrderError(PipelineInputError):
    """Device times are neither strictly increasing nor a single clean uint32 wrap."""


def normalize_records(records: Iterable[str | bytes | CSIdata]) -> tuple[tuple[CSIdata, ...], str | None]:
    """Validate every record; return read-only copies and the input kind.

    Accepts an iterable of complete `CSI_DATA` line strings (ASCII bytes are
    accepted too; no line terminator) or of CSIdata records, not both. A bare
    string/bytes/CSIdata is refused instead of being iterated character-wise.
    Empty input returns `((), None)`. The caller's objects are not modified.
    """

    if isinstance(records, (str, bytes, bytearray, CSIdata)):
        raise TypeError("records must be a sequence of lines or CSIdata records, not a single item")

    try:
        items = list(records)
    except TypeError as exc:
        raise TypeError(f"records must be iterable, got {type(records).__name__}") from exc

    kinds = set()
    normalized = []
    for index, item in enumerate(items):
        if isinstance(item, CSIdata):
            kinds.add(INPUT_CSIDATA)
        elif isinstance(item, (str, bytes, bytearray)):
            kinds.add(INPUT_RAW)
        else:
            raise TypeError(f"record {index}: expected str, bytes or CSIdata, got {type(item).__name__}")
        if len(kinds) > 1:
            raise TypeError(f"record {index}: raw lines and CSIdata records cannot be mixed in one window")

        try:
            if isinstance(item, CSIdata):
                record = validate_CSIdata(item)
            else:
                text = item.decode("ascii") if isinstance(item, (bytes, bytearray)) else item
                record = to_CSIdata(text)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RecordValidationError(index, str(exc)) from exc

        normalized.append(record)

    return tuple(normalized), (kinds.pop() if kinds else None)


def check_profile(records: tuple[CSIdata, ...]) -> None:
    """Raise `UnsupportedProfileError` unless every validated record fits the supported profile."""

    for index, record in enumerate(records):
        if record.length != CAPTURE_BYTES:
            raise UnsupportedProfileError(
                f"record {index}: CSI length {record.length} bytes; only the "
                f"{CAPTURE_BYTES}-byte profile is supported"
            )

        if record.bb_format != CAPTURE_RX_FORMAT:
            raise UnsupportedProfileError(
                f"record {index}: rx_format {record.bb_format}; only {CAPTURE_RX_FORMAT} is supported"
            )

        if not record.compensate_gain > 0:
            raise UnsupportedProfileError(
                f"record {index}: compensate_gain {record.compensate_gain} must be positive"
            )

    channels = sorted({record.channel for record in records})
    if len(channels) != 1:
        raise UnsupportedProfileError(f"channel changes inside window: {channels}; split before analysis")


def _readonly(array: np.ndarray) -> np.ndarray:
    array.setflags(write=False)
    return array


def build_session(
    records: tuple[CSIdata, ...], session_id: str = "in-memory", *, input_kind: str | None = None,
) -> tuple[SessionData, dict[str, Any]]:
    """Validated records -> (`SessionData`, timestamp policy), no files involved.

    Same contract as `load_session`: raw int8 (imag, real) bytes, complex64
    CSI, raw bin indices 0..116 (no zero bin removed), first-word flag masks
    bins 0-1, bins with no nonzero valid observation in this window are
    masked (not deleted), then packets with none. `metadata["local_timestamp"]`
    keeps the original device values; `time_s` is built from the
    `csi_adapt.resolve_timestamps` series (verbatim, or unwrapped for a single
    clean uint32 wrap), so differences and gaps are the device's own.
    Duplicate or backward times raise `TimestampOrderError`.
    `metadata_row_indices` are positions in the caller's input.
    """

    if len(records) < 2:
        raise PipelineInputError("at least two CSI packets are required")

    check_profile(records)

    device_us = [record.timestamp for record in records]
    try:
        resolved, policy = resolve_timestamps(device_us)
    except ConversionError as exc:
        raise TimestampOrderError(str(exc)) from exc

    resolved_us = np.asarray(resolved, dtype=np.int64)
    time_s = (resolved_us - resolved_us[0]).astype(np.float64) / 1_000_000.0

    n = len(records)
    raw = np.empty((n, CAPTURE_BYTES), dtype=np.int8)
    for index, record in enumerate(records):
        raw[index, 0::2] = record.data.imag.astype(np.int8)
        raw[index, 1::2] = record.data.real.astype(np.int8)

    metadata = {
        "seq": [r.seq for r in records],
        "rssi": [r.rssi for r in records],
        "noise_floor": [r.noise_floor for r in records],
        "fft_gain": [r.fft_gain for r in records],
        "agc_gain": [r.agc_gain for r in records],
        "channel": [r.channel for r in records],
        "local_timestamp": device_us,
        "sig_len": [r.sig_len for r in records],
        "rx_format": [r.bb_format for r in records],
        "len": [r.length for r in records],
        "first_word": [r.first_word_invalid for r in records],
        "compensate_gain": [r.compensate_gain for r in records],
        "dropped": [r.dropped for r in records],
    }

    metadata = {
        name: np.asarray(values, dtype=np.float64 if name == "compensate_gain" else np.int64)
        for name, values in metadata.items()
    }

    # Mask policy copied from csi_pipeline.input._load_session.
    csi = raw[:, 1::2].astype(np.complex64)
    csi.imag = raw[:, 0::2]

    sample_mask = np.ones(csi.shape, dtype=bool)
    sample_mask[metadata["first_word"].astype(bool), :2] = False
    bin_mask = np.any(sample_mask & (csi != 0), axis=0)
    sample_mask &= bin_mask[None, :]
    packet_mask = np.any(sample_mask & (csi != 0), axis=1)
    sample_mask &= packet_mask[:, None]

    bin_indices = np.arange(csi.shape[1], dtype=np.int64)
    row_indices = np.arange(n, dtype=np.int64)

    for array in (raw, csi, time_s, bin_indices, row_indices, sample_mask, bin_mask,
                  packet_mask, *metadata.values()):
        _readonly(array)

    info = {
        "n_records": n,
        "n_kept": n,
        "csi_bytes": CAPTURE_BYTES,
        "n_bins": CAPTURE_BYTES // 2,
        "source": "in-memory records (csi_respiration)",
        "input_kind": input_kind,
        "timestamp_policy": policy,
    }

    session = SessionData(
        session_id=session_id,
        path=IN_MEMORY_PATH,
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

    return session, policy
