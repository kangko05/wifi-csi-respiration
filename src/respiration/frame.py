"""The package's one input: a CSI packet, already parsed. Adapters live outside the package."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Frame:
    """One CSI packet of one link.

    t_s: wall-clock time of the packet [s] (PC arrival time). The apnea pipe's time axis.
    device_time_us: the receiver's own clock in microseconds; may wrap at 2**32. The rate
        judge's time axis, because packets can arrive in bursts.
    csi: (K,) complex64, one value per subcarrier bin as the device delivers it. Guard/DC
        bins may be zero; the rate judge drops them, the apnea pipe ignores constant bins.
    gain_key: values that change the overall receive gain in steps (ESP: AGC, FFT gain,
        TX power), or None when the device gives none. Packets with an equal key share
        one gain level in the rate judge.
    """

    t_s: float
    device_time_us: int
    csi: np.ndarray
    gain_key: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.csi.ndim != 1:
            raise ValueError(f"csi must be (K,), got shape {self.csi.shape}")
