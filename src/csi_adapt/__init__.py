"""Lossless adapters from this project's raw capture layout to other readers.

Nothing here changes `data/`. Every function reads the original
`serial.bin`/`lines.csv`/`chunks.csv`/`session.json` and writes derived files
elsewhere, keeping the original byte offsets, row indices and device times
recoverable from the derived sidecars.
"""

from .legacy_format import (
    CAPTURE_BYTES,
    ConversionError,
    ConversionReport,
    ExcludedRow,
    TimestampPolicyError,
    classify_csi_row,
    convert_session,
    decode_payload,
    resolve_timestamps,
)

__all__ = [
    "CAPTURE_BYTES",
    "ConversionError",
    "ConversionReport",
    "ExcludedRow",
    "TimestampPolicyError",
    "classify_csi_row",
    "convert_session",
    "decode_payload",
    "resolve_timestamps",
]
