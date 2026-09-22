"""Convert one raw capture into the legacy reader's `csi_raw.npy`/`meta.csv`/`session.json`.

Input contract (this project's collector, `docs/data-collection.md`):

* `serial.bin` — every byte the host read, in arrival order, never rewritten.
* `lines.csv` — one row per physical line with absolute `byte_offset`, the
  absolute `data_byte_offset`/`data_byte_length` of the base64 payload, host
  receipt time and the verbatim wire fields.
* `session.json` — status, counters, conditions and `raw_sha256` of `serial.bin`.

Output contract (`../wifi-csi-proto/src/csi_pipeline/input.py`, read-only
reference): an int8 `[packet, 234]` array of the firmware's `(imag, real)`
pairs, a `meta.csv` whose rows align 1:1 with those packets, and a
`session.json` carrying `n_records`/`n_kept`/`csi_bytes`/`n_bins`.

Preservation rules enforced here:

* Payload bytes are re-read from `serial.bin` at the recorded offsets and
  base64-decoded with `validate=True`; the decoded length must equal both the
  declared `len` and the collector's `decoded_length`. Nothing is padded.
* int8 is the signed interpretation of those bytes; byte order is untouched, so
  `raw[:, 0::2]` stays imaginary and `raw[:, 1::2]` stays real.
* Repeated `seq` values are kept. No deduplication, no interpolation, no time
  compression, no filling of gaps, no malformed row rewritten as valid zeros.
* Rows the wire parser could not parse are excluded and listed in
  `excluded_rows.csv` with their original line index, byte offset and reason.
  They are not emitted as metadata rows, because the legacy schema has no
  representation for a row whose `len` field never parsed.
* `rowmap.csv` maps every emitted packet back to its original line index, byte
  offsets, host time and unmodified device `local_timestamp`.

Device time policy: the legacy reader requires strictly increasing uint32
`local_timestamp` values and refuses to guess. Timestamps are therefore copied
verbatim whenever they already satisfy that. When a capture contains a uint32
wrap, the wrap is unwrapped and the whole series rebased onto its first packet
so that every difference is preserved exactly and the values still fit uint32.
That is an explicit, logged deviation (`timestamp_policy` in the sidecar and in
the derived `session.json`), applied only to captures that need it, and the
original values remain in `rowmap.csv`. Anything that is not a single clean
wrap raises instead of being repaired.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import numpy as np

ADAPTER_VERSION = "csi-respiration-to-legacy-capture-v1"

CAPTURE_BYTES = 234
CAPTURE_RX_FORMAT = 2
UINT32 = 1 << 32

# Legacy `meta.csv` required columns, in the firmware's wire order.
META_COLUMNS = (
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
    "compensate_gain",
    "dropped",
)

ROWMAP_COLUMNS = (
    "packet_index",
    "source_line_index",
    "source_line_byte_offset",
    "source_line_byte_length",
    "source_data_byte_offset",
    "source_data_byte_length",
    "source_decoded_length",
    "host_elapsed_s",
    "host_utc",
    "device_local_timestamp_us",
    "emitted_local_timestamp_us",
    "seq",
)

EXCLUDED_COLUMNS = (
    "source_line_index",
    "source_line_byte_offset",
    "source_line_byte_length",
    "kind",
    "parse_ok",
    "wire_error",
    "declared_len",
    "decoded_length",
    "len_match",
    "host_elapsed_s",
    "host_utc",
    "exclusion_reason",
)


class ConversionError(ValueError):
    """The capture cannot be represented in the legacy layout without guessing."""


class TimestampPolicyError(ConversionError):
    """Device times are neither strictly increasing nor a single clean wrap."""


@dataclass(frozen=True)
class ExcludedRow:
    source_line_index: int
    source_line_byte_offset: int
    source_line_byte_length: int
    kind: str
    parse_ok: str
    wire_error: str
    declared_len: str
    decoded_length: str
    len_match: str
    host_elapsed_s: str
    host_utc: str
    exclusion_reason: str


@dataclass
class ConversionReport:
    source_session_id: str
    source_dir: str
    dest_dir: str
    n_source_lines: int
    n_csi_lines: int
    n_kept_packets: int
    n_excluded_rows: int
    excluded: list[ExcludedRow] = field(default_factory=list)
    timestamp_policy: dict[str, Any] = field(default_factory=dict)
    seq_repeats: list[dict[str, Any]] = field(default_factory=list)
    interval_us: dict[str, Any] = field(default_factory=dict)
    source_sha256: dict[str, str] = field(default_factory=dict)
    derived_sha256: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["excluded"] = [asdict(row) for row in self.excluded]
        return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_lines_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames is None:
            raise ConversionError(f"{path.name}: no header")
        missing = {"line_index", "byte_offset", "kind", "parse_ok"} - set(reader.fieldnames)
        if missing:
            raise ConversionError(f"{path.name}: missing columns {sorted(missing)}")
        return list(reader)


def classify_csi_row(row: dict[str, str]) -> tuple[bool, str]:
    """Decide whether one `lines.csv` row can become a legacy metadata row.

    The verdict only uses what the collector recorded about the line itself. It
    never looks at signal content, so it is independent of any estimator.
    """
    if row["kind"] != "csi":
        return False, f"not_csi_line:{row['kind']}"
    if row["parse_ok"] != "1":
        return False, f"wire_parse_error:{row['error'] or 'unspecified'}"
    if row["error"]:
        return False, f"wire_parse_error:{row['error']}"
    if row["len_match"] != "true":
        return False, f"len_match:{row['len_match'] or 'unknown'}"
    if row["decoded_length"] != str(CAPTURE_BYTES):
        return False, f"decoded_length:{row['decoded_length']}"
    if row["len"] != str(CAPTURE_BYTES):
        return False, f"declared_len:{row['len']}"
    if row["rx_format"] != str(CAPTURE_RX_FORMAT):
        return False, f"rx_format:{row['rx_format']}"
    return True, ""


def decode_payload(raw: bytes, row: dict[str, str]) -> bytes:
    """Re-read and decode one payload from the original bytes at its offsets."""
    offset = int(row["data_byte_offset"])
    length = int(row["data_byte_length"])
    if offset < 0 or length < 0 or offset + length > len(raw):
        raise ConversionError(
            f"line {row['line_index']}: payload span {offset}+{length} outside serial.bin"
        )
    line_offset = int(row["byte_offset"])
    line_length = int(row["byte_length"])
    if not (line_offset <= offset and offset + length == line_offset + line_length):
        raise ConversionError(
            f"line {row['line_index']}: payload span is not the tail of the line span"
        )
    try:
        decoded = base64.b64decode(raw[offset : offset + length], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConversionError(f"line {row['line_index']}: base64 error: {exc}") from exc
    if len(decoded) != int(row["decoded_length"]):
        raise ConversionError(
            f"line {row['line_index']}: decoded {len(decoded)} bytes, "
            f"lines.csv recorded {row['decoded_length']}"
        )
    if len(decoded) != int(row["len"]):
        raise ConversionError(
            f"line {row['line_index']}: decoded {len(decoded)} bytes, declared len {row['len']}"
        )
    return decoded


def resolve_timestamps(
    values: list[int], *, max_wrap_interval_us: int = 1_000_000
) -> tuple[list[int], dict[str, Any]]:
    """Return legacy-writable device times plus the policy that produced them.

    `verbatim` keeps the device values byte for byte. `unwrap_uint32_rebased`
    is used only when a capture actually wraps: each wrap adds 2**32, the whole
    series is then rebased on its first packet so it still fits uint32, and
    every consecutive difference is identical to the verbatim case.
    """
    if len(values) < 2:
        raise ConversionError("need at least two packets to check device time")
    for index, value in enumerate(values):
        if not 0 <= value < UINT32:
            raise TimestampPolicyError(
                f"packet {index}: local_timestamp {value} is not a uint32 value"
            )

    diffs = np.diff(np.asarray(values, dtype=np.int64))
    if np.all(diffs > 0):
        return list(values), {
            "policy": "verbatim",
            "reason": "device local_timestamp is already strictly increasing",
            "wraps": [],
            "rebase_subtracted_us": 0,
        }

    unwrapped: list[int] = []
    wraps: list[dict[str, Any]] = []
    offset = 0
    previous: int | None = None
    for index, value in enumerate(values):
        current = value + offset
        if previous is not None and current <= previous:
            candidate = current + UINT32
            interval = candidate - previous
            if not 0 < interval <= max_wrap_interval_us:
                raise TimestampPolicyError(
                    f"packet {index}: local_timestamp {values[index]} follows {values[index - 1]} "
                    f"and is not a single uint32 wrap (implied interval {interval} us); "
                    "no automatic time recovery was applied"
                )
            offset += UINT32
            current = candidate
            wraps.append(
                {
                    "packet_index": index,
                    "previous_device_us": int(values[index - 1]),
                    "device_us": int(values[index]),
                    "implied_interval_us": int(interval),
                }
            )
        previous = current
        unwrapped.append(current)

    base = unwrapped[0]
    rebased = [value - base for value in unwrapped]
    if rebased[-1] >= UINT32:
        raise TimestampPolicyError(
            "unwrapped device time span exceeds uint32; the legacy meta.csv schema "
            "cannot carry it without changing the reader"
        )
    return rebased, {
        "policy": "unwrap_uint32_rebased",
        "reason": (
            "device local_timestamp wrapped the 32-bit microsecond counter; each wrap adds "
            "2**32 and the series is rebased on its first packet. Consecutive differences are "
            "unchanged; original device values are kept in rowmap.csv."
        ),
        "wraps": wraps,
        "rebase_subtracted_us": int(base),
    }


def _write_csv(path: Path, columns, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def convert_session(
    source: Path,
    dest: Path,
    *,
    annotations: dict[str, Any] | None = None,
) -> ConversionReport:
    """Write the legacy trio plus sidecars for one capture directory."""
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if dest == source or dest.is_relative_to(source):
        raise ConversionError("derived directory must be outside the original capture")
    dest.mkdir(parents=True, exist_ok=False)

    info = json.loads((source / "session.json").read_text(encoding="utf-8"))
    raw_path = source / "serial.bin"
    raw = raw_path.read_bytes()
    raw_digest = hashlib.sha256(raw).hexdigest()
    if info.get("raw_sha256") and info["raw_sha256"] != raw_digest:
        raise ConversionError(
            f"{source.name}: serial.bin sha256 {raw_digest} does not match the manifest"
        )
    if info.get("raw_bytes_on_disk") not in (None, len(raw)):
        raise ConversionError(f"{source.name}: serial.bin length does not match the manifest")

    lines = read_lines_csv(source / "lines.csv")
    kept: list[dict[str, str]] = []
    excluded: list[ExcludedRow] = []
    for row in lines:
        usable, reason = classify_csi_row(row)
        if usable:
            kept.append(row)
        else:
            excluded.append(
                ExcludedRow(
                    source_line_index=int(row["line_index"]),
                    source_line_byte_offset=int(row["byte_offset"]),
                    source_line_byte_length=int(row["byte_length"]),
                    kind=row["kind"],
                    parse_ok=row["parse_ok"],
                    wire_error=row["error"],
                    declared_len=row["len"],
                    decoded_length=row["decoded_length"],
                    len_match=row["len_match"],
                    host_elapsed_s=row["host_elapsed_s"],
                    host_utc=row["host_utc"],
                    exclusion_reason=reason,
                )
            )
    if len(kept) < 2:
        raise ConversionError(f"{source.name}: fewer than two usable CSI packets")

    channels = {row["channel"] for row in kept}
    if len(channels) != 1:
        raise ConversionError(f"{source.name}: channel changes inside capture: {sorted(channels)}")

    array = np.empty((len(kept), CAPTURE_BYTES), dtype=np.int8)
    for index, row in enumerate(kept):
        array[index] = np.frombuffer(decode_payload(raw, row), dtype=np.int8)

    device_us = [int(row["local_timestamp"]) for row in kept]
    emitted_us, policy = resolve_timestamps(device_us)

    seen: dict[int, int] = {}
    repeats: list[dict[str, Any]] = []
    for index, row in enumerate(kept):
        seq = int(row["seq"])
        if seq in seen:
            repeats.append(
                {
                    "seq": seq,
                    "first_packet_index": seen[seq],
                    "repeat_packet_index": index,
                    "first_device_us": device_us[seen[seq]],
                    "repeat_device_us": device_us[index],
                    "identical_payload": bool(
                        np.array_equal(array[seen[seq]], array[index])
                    ),
                }
            )
        else:
            seen[seq] = index

    np.save(dest / "csi_raw.npy", array, allow_pickle=False)
    _write_csv(
        dest / "meta.csv",
        META_COLUMNS,
        (
            {name: (str(emitted_us[index]) if name == "local_timestamp" else row[name])
             for name in META_COLUMNS}
            for index, row in enumerate(kept)
        ),
    )
    _write_csv(
        dest / "rowmap.csv",
        ROWMAP_COLUMNS,
        (
            {
                "packet_index": index,
                "source_line_index": row["line_index"],
                "source_line_byte_offset": row["byte_offset"],
                "source_line_byte_length": row["byte_length"],
                "source_data_byte_offset": row["data_byte_offset"],
                "source_data_byte_length": row["data_byte_length"],
                "source_decoded_length": row["decoded_length"],
                "host_elapsed_s": row["host_elapsed_s"],
                "host_utc": row["host_utc"],
                "device_local_timestamp_us": device_us[index],
                "emitted_local_timestamp_us": emitted_us[index],
                "seq": row["seq"],
            }
            for index, row in enumerate(kept)
        ),
    )
    _write_csv(dest / "excluded_rows.csv", EXCLUDED_COLUMNS, (asdict(row) for row in excluded))

    intervals = np.diff(np.asarray(emitted_us, dtype=np.int64))
    interval_us = {
        "min": int(intervals.min()),
        "median": float(np.median(intervals)),
        "max": int(intervals.max()),
        "span_s": (emitted_us[-1] - emitted_us[0]) / 1e6,
        "n_intervals_over_100ms": int((intervals > 100_000).sum()),
    }

    derived_info = {
        "schema": 1,
        "artifact_kind": ADAPTER_VERSION,
        "session_id": source.name,
        "n_records": len(kept),
        "n_kept": len(kept),
        "csi_bytes": CAPTURE_BYTES,
        "n_bins": CAPTURE_BYTES // 2,
        "derived_from": {
            "source_session_id": info.get("session_id"),
            "source_dir": str(source),
            "source_raw_sha256": raw_digest,
            "source_status": info.get("status"),
            "source_counters": info.get("counters"),
            "source_timing": info.get("timing"),
            "source_conditions": info.get("conditions"),
            "source_unknown_conditions": info.get("unknown_conditions"),
        },
        "conditions_note": (
            "Conditions are operator-entered context copied verbatim from the original "
            "manifest. They are never used as ground truth by any processing and contain "
            "no manual breath counts."
        ),
        "interpretation": {
            "byte_order_preserved": True,
            "iq_decoding": "int8 (imag, real) pairs, as stored by the firmware",
            "signed_interpretation": "signed int8",
            "resampled": False,
            "deduplicated": False,
            "gap_filled": False,
            "zero_filled_malformed_rows": False,
        },
        "timestamp_policy": policy,
        "excluded_rows": {
            "n": len(excluded),
            "file": "excluded_rows.csv",
            "note": (
                "Rows the wire parser could not parse have no representable `len` field, so "
                "they are not emitted as legacy metadata rows. Their original line index, "
                "byte offsets, host time and error string are preserved in excluded_rows.csv."
            ),
        },
        "seq_repeats": repeats,
        "row_mapping_file": "rowmap.csv",
        "labels": {
            "manual_reference_recorded": False,
            "note": "No manual breath counts exist for this capture; none were created here.",
        },
        "annotations": annotations or {},
    }
    (dest / "session.json").write_text(
        json.dumps(derived_info, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )

    report = ConversionReport(
        source_session_id=source.name,
        source_dir=str(source),
        dest_dir=str(dest),
        n_source_lines=len(lines),
        n_csi_lines=sum(row["kind"] == "csi" for row in lines),
        n_kept_packets=len(kept),
        n_excluded_rows=len(excluded),
        excluded=excluded,
        timestamp_policy=policy,
        seq_repeats=repeats,
        interval_us=interval_us,
        source_sha256={
            name: sha256_file(source / name)
            for name in ("serial.bin", "chunks.csv", "lines.csv", "session.json")
        },
        derived_sha256={
            name: sha256_file(dest / name)
            for name in ("csi_raw.npy", "meta.csv", "session.json", "rowmap.csv", "excluded_rows.csv")
        },
        notes=list((annotations or {}).get("notes", [])),
    )
    (dest / "adapter_sidecar.json").write_text(
        json.dumps(report.to_json(), indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return report
