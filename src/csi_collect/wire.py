"""Parsing of the RX console wire format, without altering any received bytes.

The RX firmware prints one CSV line per CSI packet (`csi-rx/main/csi-rx.c:234-245`):

    type,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,
    sig_len,rx_format,len,first_word,compensate_gain,dropped,data

`data` is base64 of the raw CSI bytes as stored by the firmware (int8 buffer,
`CSI_BUF_MAX` 512 bytes; the valid length is `len`). This module never decodes
I/Q, never selects subcarriers and never repairs values: it only records what a
line says, whether it is syntactically parseable, and where its bytes live.
"""

from __future__ import annotations

import base64
import binascii
import math
from dataclasses import dataclass, field

# Wire metadata columns in firmware order; `first_word` carries
# `first_word_invalid`, a firmware flag that is kept as a flag.
WIRE_FIELDS = (
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

CSI_PREFIX = b"CSI_DATA,"
HEADER_PREFIX = b"type,seq,"

KIND_CSI = "csi"
KIND_HEADER = "header"
KIND_OTHER = "other"


@dataclass
class ParsedLine:
    """Result of inspecting one physical line.

    `parse_ok` means the line is syntactically well formed, nothing more. It is
    not a statement about CSI sample validity: see `first_word` (the firmware's
    `first_word_invalid` flag) and the deferred signed/IQ interpretation.
    """

    kind: str
    parse_ok: bool = False
    error: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    # Offsets are relative to the start of the line content.
    data_rel_offset: int = -1
    data_length: int = -1
    decoded_length: int = -1
    len_match: str = ""


def parse_line(content: bytes) -> ParsedLine:
    """Classify and parse one line's content (terminator bytes excluded)."""
    if content.startswith(HEADER_PREFIX):
        return ParsedLine(kind=KIND_HEADER, parse_ok=True)
    if not content.startswith(CSI_PREFIX):
        return ParsedLine(kind=KIND_OTHER, parse_ok=True)

    parsed = ParsedLine(kind=KIND_CSI)
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError:
        parsed.error = "non_ascii"
        return parsed

    parts = text.split(",")
    if len(parts) != len(WIRE_FIELDS) + 2:
        parsed.error = f"field_count:{len(parts)}"
        return parsed

    # Original strings are kept verbatim; numeric checks only add a verdict.
    parsed.fields = dict(zip(WIRE_FIELDS, parts[1:-1]))
    data_text = parts[-1]
    parsed.data_length = len(data_text)
    parsed.data_rel_offset = len(content) - len(data_text)

    errors = []
    for name, value in parsed.fields.items():
        try:
            number = float(value) if name == "compensate_gain" else int(value)
        except ValueError:
            errors.append(f"bad_{name}")
            continue
        # "nan"/"inf" parse as floats but are not usable gain values; they are
        # reported as parse errors. The original string stays in `fields`.
        if name == "compensate_gain" and not math.isfinite(number):
            errors.append(f"nonfinite_{name}")

    try:
        decoded = base64.b64decode(data_text, validate=True)
    except (binascii.Error, ValueError):
        errors.append("base64_error")
    else:
        parsed.decoded_length = len(decoded)
        try:
            declared = int(parsed.fields["len"])
        except ValueError:
            parsed.len_match = "unknown"
        else:
            match = declared == parsed.decoded_length
            parsed.len_match = "true" if match else "false"
            if not match:
                errors.append(f"len_mismatch:{declared}!={parsed.decoded_length}")

    parsed.error = ";".join(errors)
    parsed.parse_ok = not errors
    return parsed
