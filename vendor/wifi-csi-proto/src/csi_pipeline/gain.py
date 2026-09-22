"""Stage 2: alternative gain corrections, each starting from raw SessionData.

The capture firmware records get_gain_compensation()'s coefficient but copies
CSI bytes unchanged. Multiply by that coefficient once; do not divide.
No paper-based correction, resampling, filtering, or phase calibration here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .input import SessionData


@dataclass(frozen=True)
class GainConfig:
    method: Literal["none", "firmware"] = "none"

    def __post_init__(self) -> None:
        if self.method not in ("none", "firmware"):
            raise ValueError(f"unsupported gain method: {self.method!r}")


@dataclass(frozen=True)
class GainResult:
    """Corrected CSI plus its unchanged input and explicit applied factors.

    csi: complex128 [packet, raw bin]; applied_gain: float64 [packet].
    Access original time, masks and metadata through source. Invalid positions
    remain masked in source, not removed or replaced. Arrays are read-only.
    Firmware readiness is unknown: the capture did not save its return status.
    """

    source: SessionData
    config: GainConfig
    csi: NDArray[np.complex128]
    applied_gain: NDArray[np.float64]


def correct_gain(
    session: SessionData, config: GainConfig = GainConfig()
) -> GainResult:
    """Return a new result; accept raw SessionData only to avoid double correction."""
    if not isinstance(session, SessionData):
        raise TypeError("correct_gain expects raw SessionData, not a gain result")
    if not isinstance(config, GainConfig):
        raise TypeError("config must be GainConfig")
    if config.method == "none":
        gain = np.ones(len(session.time_s), dtype=np.float64)
    else:
        gain = np.array(session.metadata["compensate_gain"], dtype=np.float64, copy=True)
    if gain.shape != (session.csi.shape[0],):
        raise ValueError("gain must have one coefficient per aligned CSI packet")
    if not np.all(np.isfinite(gain) & (gain > 0)):
        raise ValueError("gain coefficients must be finite and positive")
    # Float arithmetic avoids int8 clipping/rounding after compensation.
    try:
        with np.errstate(over="raise", invalid="raise"):
            csi = session.csi.astype(np.complex128) * gain[:, None]
    except FloatingPointError as exc:
        raise ValueError("gain correction overflowed") from exc
    if not np.all(np.isfinite(csi)):
        raise ValueError("gain correction produced non-finite CSI")
    csi.setflags(write=False)
    gain.setflags(write=False)
    return GainResult(source=session, config=config, csi=csi, applied_gain=gain)
