"""Resample onto a uniform time grid.

Mandatory regardless of how good the firmware gets: wireless delivery is
non-deterministic, so packet arrival is never uniform, and every spectral
estimator downstream assumes it is.
"""

from __future__ import annotations

import numpy as np

from ..model import CsiBlock


def to_uniform(block: CsiBlock, fs: float | None = None) -> tuple[CsiBlock, float]:
    """Linearly interpolate onto a uniform grid; returns the block and fs.

    Real and imaginary parts are interpolated separately.  Interpolating
    magnitude and phase instead breaks wherever the phase wraps.
    """
    if block.n_packets < 2:
        raise ValueError("need at least two packets to resample")

    if fs is None:
        # Median interval is robust to the occasional long gap, unlike the mean.
        fs = 1.0 / float(np.median(np.diff(block.t)))

    n = int(np.floor(block.duration * fs)) + 1
    grid = block.t[0] + np.arange(n) / fs

    H = np.empty((n, block.n_subcarriers), dtype=complex)
    for k in range(block.n_subcarriers):
        H[:, k] = (np.interp(grid, block.t, block.H[:, k].real)
                   + 1j * np.interp(grid, block.t, block.H[:, k].imag))

    scalar = lambda v: np.interp(grid, block.t, v)  # noqa: E731
    resampled = CsiBlock(
        t=grid,
        seq=np.round(scalar(block.seq)).astype(np.int64),
        H=H,
        f=block.f,
        rssi=scalar(block.rssi),
        noise_floor=scalar(block.noise_floor),
        agc_gain=scalar(block.agc_gain),
        fft_gain=scalar(block.fft_gain),
        compensate_gain=scalar(block.compensate_gain),
        dropped=np.round(scalar(block.dropped)).astype(np.int64),
    )
    return resampled, fs


def gap_report(block: CsiBlock) -> dict:
    """Interval statistics, to tell a healthy capture from a gappy one."""
    d = np.diff(block.t)
    if d.size == 0:
        return {}
    return {
        "median_interval": float(np.median(d)),
        "p95_interval": float(np.percentile(d, 95)),
        "max_interval": float(d.max()),
        "jitter_std": float(d.std()),
        "mean_rate": block.mean_rate,
        "loss_ratio": block.loss_ratio,
        "queue_drops": int(block.dropped[-1] - block.dropped[0]),
    }
