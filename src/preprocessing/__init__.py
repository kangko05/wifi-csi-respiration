"""
해당 모듈은 esp32c5의 데이터를 문자열로 받아 파싱하여 각 패킷의 메타데이터와 csi 데이터로 반환한다.

Wire line (`csi-rx/main/csi-rx.c`), 15 comma-separated fields:

    CSI_DATA,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,
    sig_len,rx_format,len,first_word,compensate_gain,dropped,<base64>

`data` holds the decoded int8 buffer as complex128, imag-first on the wire:
byte[2k] is imag, byte[2k+1] is real. Invalid lines raise ValueError; nothing
is padded, truncated or replaced by zeros. `validate_CSIdata` is the single
record check shared by this parser and by callers that construct CSIdata
themselves (`csi_respiration`).
"""

from dataclasses import dataclass, replace
import base64
import binascii
import math
import re

import numpy as np
from numpy.typing import NDArray

CSI_LABEL = "CSI_DATA"
CSI_DATA_LENGTH = 15
UINT32 = 1 << 32
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1  # downstream metadata arrays are int64

# Firmware prints %d/%u/%f; stricter than int()/float(), which accept spaces and "_".
_INT_RE = re.compile(r"-?[0-9]+")
_FLOAT_RE = re.compile(r"-?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?")

INT_FIELDS = (
    "seq", "timestamp", "dropped", "rssi", "noise_floor", "fft_gain", "agc_gain",
    "channel", "bb_format", "first_word_invalid", "sig_len", "length",
)


@dataclass
class CSIdata:
    seq: int
    timestamp: int  # device local_timestamp, uint32 microseconds
    dropped: int
    rssi: int
    noise_floor: int
    fft_gain: int
    agc_gain: int
    channel: int
    bb_format: int  # wire rx_format
    first_word_invalid: int  # 0/1 firmware flag
    sig_len: int
    length: int  # valid bytes in buf == 2 * len(data)

    data: NDArray  # b64 decoded complex128 array, one value per int8 (imag, real) pair
    compensate_gain: float


def _parse_int(text: str, name: str) -> int:
    if not _INT_RE.fullmatch(text):
        raise ValueError(f"invalid data string: {name}={text!r} is not an integer")

    return int(text)


def _parse_float(text: str, name: str) -> float:
    if not _FLOAT_RE.fullmatch(text):
        raise ValueError(f"invalid data string: {name}={text!r} is not a number")

    return float(text)


def validate_CSIdata(record: CSIdata) -> CSIdata:
    """Check one record and return a normalized copy; never modifies `record`.

    Metadata must be integers representable as int64 (bool rejected; numpy
    integers become int), `timestamp` a uint32, `first_word_invalid` 0 or 1,
    `compensate_gain` a finite float. `data` must be a 1-D complex array whose real and imaginary parts
    are int8 values, with `length == 2 * data.size`. The returned `data` is a
    read-only complex128 copy.
    """

    if not isinstance(record, CSIdata):
        raise TypeError(f"expected CSIdata, got {type(record).__name__}")

    values = {}
    for name in INT_FIELDS:
        value = getattr(record, name)
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise ValueError(f"{name} must be an integer, got {type(value).__name__}")

        values[name] = int(value)
        if not INT64_MIN <= values[name] <= INT64_MAX:
            raise ValueError(f"{name} {values[name]} is outside the int64 range")

    if not 0 <= values["timestamp"] < UINT32:
        raise ValueError(f"timestamp {values['timestamp']} is not uint32 microseconds")
    if values["first_word_invalid"] not in (0, 1):
        raise ValueError("first_word_invalid must be 0 or 1")

    gain = record.compensate_gain
    if isinstance(gain, (bool, np.bool_)) or not isinstance(gain, (int, float, np.integer, np.floating)):
        raise ValueError(f"compensate_gain must be a real number, got {type(gain).__name__}")

    try:
        gain = float(gain)
    except OverflowError as exc:  # Python int too large for a float
        raise ValueError(f"compensate_gain {gain} does not fit a float") from exc

    if not math.isfinite(gain):
        raise ValueError(f"compensate_gain {gain!r} is not finite")

    data = record.data
    if not isinstance(data, np.ndarray) or not np.issubdtype(data.dtype, np.complexfloating):
        raise ValueError("data must be a complex numpy array")

    if data.ndim != 1:
        raise ValueError(f"data must be one-dimensional, got shape {data.shape}")

    if values["length"] != 2 * data.size:
        raise ValueError(f"length {values['length']} != 2 * {data.size} decoded CSI values")

    parts = np.concatenate([data.real, data.imag])
    if not np.all(np.isfinite(parts)):
        raise ValueError("data contains non-finite values")
    if np.any(parts != np.round(parts)) or np.any(parts < -128) or np.any(parts > 127):
        raise ValueError("data real/imag parts must be int8 values")

    data = np.array(data, dtype=np.complex128, copy=True)
    data.setflags(write=False)

    return replace(record, compensate_gain=gain, data=data, **values)


# esp_data_str = 패킷의 메타데이터 + raw csi data
# returns CSIdata
def to_CSIdata(esp_data_str: str) -> CSIdata:
    if not isinstance(esp_data_str, str):
        raise TypeError(f"expected str, got {type(esp_data_str).__name__}")

    sp = esp_data_str.split(",", 14)

    if len(sp) < CSI_DATA_LENGTH or sp[0] != CSI_LABEL:
        raise ValueError("invalid data string")

    names = ("seq", "rssi", "noise_floor", "fft_gain", "agc_gain", "channel", "timestamp",
             "sig_len", "bb_format", "length", "first_word_invalid")
    fields = {name: _parse_int(text, name) for name, text in zip(names, sp[1:12])}
    fields["compensate_gain"] = _parse_float(sp[12], "compensate_gain")
    fields["dropped"] = _parse_int(sp[13], "dropped")

    try:
        buf = base64.b64decode(sp[14], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid data string: base64 error: {exc}") from exc

    if len(buf) != fields["length"]:
        raise ValueError(f"invalid data string: decoded {len(buf)} bytes, declared len {fields['length']}")
    if len(buf) % 2:
        raise ValueError(f"invalid data string: odd decoded byte count {len(buf)}")

    raw = np.frombuffer(buf, dtype=np.int8)
    csi = np.empty(len(raw) // 2, dtype=np.complex128)
    csi.real, csi.imag = raw[1::2], raw[0::2]

    return validate_CSIdata(CSIdata(data=csi, **fields))


if __name__ == "__main__":
    td = "CSI_DATA,384306,-32,-94,20,18,36,3515310213,47,2,234,0,0.266073,0,CgsLDQwNCQ8KEAkRBhEJEQYUBhMHFQcUBRYFFQMVAhQDFgEVAhcAFQEVABYAFAMYAhYEFwEXABT/GAEYARf9GP4Y/xb/G/4bABoBGgAaAhoCGwAbAB0BGwMcAhwDHQMdBBwCHwYkBx8EIgMkBSUEJQQjAAAAAAAAJv0o+yb9Jf0k/iP+Jv0k/yX8Iv4j/SP8JP4h/ST8Ifsi/CD8IwAg/iD/IAEfACAAHQEfBBwCHv0dAhz/GwEcBRsGGgcYAxsIHAcZBhkHFgkXBBcGGAYYBRYIFgkWCRYIFgkVCRMLEgsSDBEMEg4OCwwN"

    print(to_CSIdata(td))
