"""Outlier removal and band-pass filtering for the breathing band."""

from __future__ import annotations

import numpy as np
from scipy import signal

# Search band, 3-60 breaths/min.  Wider than the 0.1-0.6 Hz usually quoted for
# resting adults because both ends are in scope here: hyperventilation runs to
# 40+ bpm, and a measured capture at 5.0 bpm sat below a 0.1 Hz floor, where
# the band-pass strips the fundamental and leaves the second harmonic (that
# capture read 11.4 bpm from peak counting against a true 5.0).
# Cost of the low edge: more drift and 1/f energy enters, and at 0.05 Hz a 60 s
# record holds only 3 cycles, so the spectral line is inherently broad (~1 bpm).
BAND_LOW = 0.05
BAND_HIGH = 1.0


def hampel(x: np.ndarray, window: int = 11, n_sigma: float = 3.0) -> np.ndarray:
    """Replace outliers with the local median (rolling median/MAD)."""
    x = np.asarray(x, dtype=float)
    if x.size < window or window < 3:
        return x.copy()

    half = window // 2
    padded = np.pad(x, half, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, window)

    med = np.median(view, axis=-1)
    mad = np.median(np.abs(view - med[:, None]), axis=-1)
    # 1.4826 makes the MAD a consistent estimator of sigma for Gaussian data.
    threshold = n_sigma * 1.4826 * mad

    out = x.copy()
    bad = np.abs(x - med) > threshold
    out[bad] = med[bad]
    return out


def bandpass(x: np.ndarray, fs: float, low: float = BAND_LOW,
             high: float = BAND_HIGH, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth band-pass, second-order sections.

    SOS, never b/a: a narrow low-frequency b/a design is numerically unstable
    and blows the output up by orders of magnitude.
    """
    nyq = fs / 2
    high = min(high, nyq * 0.99)
    if not 0 < low < high < nyq:
        raise ValueError(f"band {low}-{high} Hz invalid for fs={fs}")

    sos = signal.butter(order, [low / nyq, high / nyq], btype="band", output="sos")
    # sosfiltfilt needs a few multiples of the filter's settling length.
    if x.shape[-1] <= 3 * order * 2:
        raise ValueError(f"signal too short ({x.shape[-1]}) for order {order}")
    return signal.sosfiltfilt(sos, x, axis=-1)


def detrend(x: np.ndarray, fs: float, cutoff: float = BAND_LOW) -> np.ndarray:
    """Remove slow drift below the breathing band."""
    nyq = fs / 2
    sos = signal.butter(2, cutoff / nyq, btype="high", output="sos")
    return signal.sosfiltfilt(sos, x, axis=-1)


def savgol(x: np.ndarray, window: int = 31, order: int = 3) -> np.ndarray:
    """Smooth high-frequency noise while keeping peak shape."""
    window = min(window, x.shape[-1] if x.shape[-1] % 2 else x.shape[-1] - 1)
    if window <= order:
        return x.copy()
    return signal.savgol_filter(x, window, order, axis=-1)
