"""Core data container passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

# rx_ctrl.timestamp is a 32-bit microsecond counter, so it wraps every ~71.6 min.
TIMESTAMP_MODULO = 1 << 32


@dataclass
class CsiBlock:
    """A batch of CSI frames on a common subcarrier grid.

    ``H`` is ``[P, K]`` complex, one row per packet.  ``t`` is in seconds,
    derived from the hardware capture timestamp (already unwrapped), so it is
    irregularly spaced until :func:`csi.preprocess.resample.to_uniform` runs.
    ``f`` is the ``[K]`` subcarrier frequency offset in Hz, needed by the
    timing-offset correction.
    """

    t: np.ndarray  # [P] float64 seconds
    seq: np.ndarray  # [P] int64, tx-side counter
    H: np.ndarray  # [P, K] complex128
    f: np.ndarray  # [K] float64 Hz, offset from carrier
    rssi: np.ndarray  # [P] int
    noise_floor: np.ndarray  # [P] int
    agc_gain: np.ndarray  # [P] int
    fft_gain: np.ndarray  # [P] int
    compensate_gain: np.ndarray  # [P] float, as reported by the firmware
    dropped: np.ndarray  # [P] int, cumulative queue overflow count

    def __post_init__(self) -> None:
        p, k = self.H.shape
        if self.f.shape != (k,):
            raise ValueError(f"f has {self.f.shape}, expected ({k},)")
        for name in ("t", "seq", "rssi", "noise_floor", "agc_gain",
                     "fft_gain", "compensate_gain", "dropped"):
            got = getattr(self, name).shape
            if got != (p,):
                raise ValueError(f"{name} has {got}, expected ({p},)")

    @property
    def n_packets(self) -> int:
        return self.H.shape[0]

    @property
    def n_subcarriers(self) -> int:
        return self.H.shape[1]

    @property
    def duration(self) -> float:
        return float(self.t[-1] - self.t[0]) if self.n_packets > 1 else 0.0

    @property
    def mean_rate(self) -> float:
        """Average packets per second actually delivered."""
        return (self.n_packets - 1) / self.duration if self.duration > 0 else 0.0

    @property
    def loss_ratio(self) -> float:
        """Fraction of tx packets that never arrived, from the seq counter."""
        if self.n_packets < 2:
            return 0.0
        expected = int(self.seq[-1] - self.seq[0]) + 1
        return 1.0 - self.n_packets / expected if expected > 0 else 0.0

    def with_H(self, H: np.ndarray, **kw) -> "CsiBlock":
        """Copy with a new CSI matrix; stages stay pure by going through this."""
        return replace(self, H=H, **kw)

    def select_subcarriers(self, mask: np.ndarray) -> "CsiBlock":
        return replace(self, H=self.H[:, mask], f=self.f[mask])

    def select_packets(self, idx: np.ndarray) -> "CsiBlock":
        return replace(
            self,
            t=self.t[idx],
            seq=self.seq[idx],
            H=self.H[idx],
            rssi=self.rssi[idx],
            noise_floor=self.noise_floor[idx],
            agc_gain=self.agc_gain[idx],
            fft_gain=self.fft_gain[idx],
            compensate_gain=self.compensate_gain[idx],
            dropped=self.dropped[idx],
        )


def unwrap_timestamps(raw: np.ndarray) -> np.ndarray:
    """Undo the 32-bit microsecond wraparound, returning seconds.

    A capture longer than ~71.6 minutes, or one that straddles a wrap, would
    otherwise jump backwards and destroy the time base.
    """
    raw = np.asarray(raw, dtype=np.int64)
    if raw.size == 0:
        return np.zeros(0)
    steps = np.diff(raw)
    wraps = np.cumsum(np.concatenate([[0], (steps < 0).astype(np.int64)]))
    return (raw + wraps * TIMESTAMP_MODULO - raw[0]) / 1e6
