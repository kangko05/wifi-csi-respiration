"""
해당 모듈은 esp32c5의 데이터를 문자열로 받아 파싱하여 각 패킷의 메타데이터와 csi 데이터로 반환한다.
"""

from dataclasses import dataclass
import base64
import numpy as np
from numpy.typing import NDArray

CSI_LABEL = "CSI_DATA"
CSI_DATA_LENGTH = 15


@dataclass
class CSIdata:
    seq: int
    timestamp: int
    dropped: int
    rssi: int
    noise_floor: int
    fft_gain: int
    agc_gain: int
    channel: int
    bb_format: int
    first_word_invalid: int
    sig_len: int
    length: int  # valid bytes in buf

    data: NDArray  # b64 decoded complex array
    compensate_gain: float


# esp_data_str = 패킷의 메타데이터 + raw csi data
# returns CSIdata
def to_CSIdata(esp_data_str: str) -> CSIdata:
    sp = esp_data_str.split(",", 14)

    if len(sp) < CSI_DATA_LENGTH or sp[0] != CSI_LABEL:
        raise ValueError("invalid data string")

    sp[1:12] = map(int, sp[1:12])
    sp[12] = float(sp[12])  # compensate_gain
    sp[13] = int(sp[13])  # dropped

    buf = base64.b64decode(sp[14], validate=True)
    raw = np.frombuffer(buf, dtype=np.int8)
    csi = np.empty(len(raw) // 2, dtype=np.complex128)
    csi.real, csi.imag = raw[1::2], raw[0::2]

    return CSIdata(
        seq=sp[1],
        rssi=sp[2],
        noise_floor=sp[3],
        fft_gain=sp[4],
        agc_gain=sp[5],
        channel=sp[6],
        timestamp=sp[7],
        sig_len=sp[8],
        bb_format=sp[9],
        length=sp[10],
        first_word_invalid=sp[11],
        compensate_gain=sp[12],
        dropped=sp[13],
        data=csi,
    )


if __name__ == "__main__":
    td = "CSI_DATA,384306,-32,-94,20,18,36,3515310213,47,2,234,0,0.266073,0,CgsLDQwNCQ8KEAkRBhEJEQYUBhMHFQcUBRYFFQMVAhQDFgEVAhcAFQEVABYAFAMYAhYEFwEXABT/GAEYARf9GP4Y/xb/G/4bABoBGgAaAhoCGwAbAB0BGwMcAhwDHQMdBBwCHwYkBx8EIgMkBSUEJQQjAAAAAAAAJv0o+yb9Jf0k/iP+Jv0k/yX8Iv4j/SP8JP4h/ST8Ifsi/CD8IwAg/iD/IAEfACAAHQEfBBwCHv0dAhz/GwEcBRsGGgcYAxsIHAcZBhkHFgkXBBcGGAYYBRYIFgkWCRYIFgkVCRMLEgsSDBEMEg4OCwwN"

    print(to_CSIdata(td))
