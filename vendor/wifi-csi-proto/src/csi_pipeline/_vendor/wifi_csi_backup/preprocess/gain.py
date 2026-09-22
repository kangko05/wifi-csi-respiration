"""Gain-error correction.

Produces g_hat[P] such that H / g_hat[:, None] is gain-corrected.  Three
methods share the interface so they can be A/B'd on one capture.

Reference: Ratnam et al., "Optimal Preprocessing of WiFi CSI for Sensing
Applications", IEEE TWC 2024, Section III.
"""

from __future__ import annotations

import numpy as np

from ..model import CsiBlock


def firmware_agc(block: CsiBlock) -> np.ndarray:
    """Use the AGC/FFT gain the ESP32 reports per packet.

    Commodity NICs do not expose the AGC index, which is why the paper has to
    estimate the gain grid; we get it directly, so this is a lookup.  The
    firmware reports compensate_gain as a multiplier, hence the reciprocal.
    """
    g = np.asarray(block.compensate_gain, dtype=float)
    g = np.where(g > 0, g, 1.0)
    return 1.0 / g


def rms_norm(block: CsiBlock) -> np.ndarray:
    """Ratnam eq (6): attribute all CSI power variation to the gain error.

    Discards the sensing signal carried in the amplitude, but also every bit of
    gain noise.  The paper finds this wins whenever the dynamic component is
    weak (gamma > 0.95), which is exactly the stationary-subject case.
    """
    return np.sqrt(np.mean(np.abs(block.H) ** 2, axis=1))


def _dbscan_1d(x: np.ndarray, eps: float) -> np.ndarray:
    """Cluster labels for 1-D points, min_points=1 (so nothing is noise).

    With min_points=1 DBSCAN on a line reduces to splitting the sorted values
    wherever consecutive points are more than eps apart.
    """
    order = np.argsort(x)
    labels = np.empty(x.size, dtype=np.int64)
    gaps = np.diff(x[order]) > eps
    labels[order] = np.concatenate([[0], np.cumsum(gaps)])
    return labels


def _lpf_moving_average(x: np.ndarray, half_width: int) -> np.ndarray:
    """Moving average with a one-sided width, edge-padded (paper's LPF_0.1Hz)."""
    if half_width < 1:
        return x.astype(float)
    n = 2 * half_width + 1
    padded = np.pad(x.astype(float), half_width, mode="edge")
    return np.convolve(padded, np.ones(n) / n, mode="valid")


def agc_grid_ml(block: CsiBlock, rep_interval: float | None = None,
                eps: float = 0.2) -> np.ndarray:
    """Ratnam Algorithm 1/3: cluster gain *increments*, then low-pass the rest.

    Clustering the increments rather than the levels is what makes this immune
    to slow drift in the large-scale gain.
    """
    gamma_t = 10 * np.log10(np.mean(np.abs(block.H) ** 2, axis=1))
    delta = np.diff(gamma_t, prepend=gamma_t[0])

    labels = _dbscan_1d(delta[1:], eps)
    step = np.zeros_like(delta)
    for i, lab in enumerate(labels, start=1):
        step[i] = delta[1:][labels == lab].mean()

    g2 = np.cumsum(step)  # AGC gain, eq (12)

    if rep_interval is None:
        rep_interval = 1.0 / block.mean_rate if block.mean_rate > 0 else 0.02
    g1 = _lpf_moving_average(gamma_t - g2, int(round(6 / rep_interval)))

    return 10 ** ((g1 + g2) / 20)


METHODS = {
    "firmware_agc": firmware_agc,
    "rms_norm": rms_norm,
    "agc_grid_ml": agc_grid_ml,
}


def correct(block: CsiBlock, method: str = "rms_norm") -> CsiBlock:
    if method not in METHODS:
        raise ValueError(f"unknown gain method {method!r}; have {sorted(METHODS)}")
    g = METHODS[method](block)
    g = np.where(np.abs(g) > 0, g, 1.0)
    return block.with_H(block.H / g[:, None])
