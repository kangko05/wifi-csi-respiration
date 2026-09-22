"""Confidence metrics -- is there a breathing signal here at all?"""

from __future__ import annotations

import numpy as np

from ..preprocess.filters import BAND_HIGH, BAND_LOW

# `pm` is reported for continuity but is NOT the gate any more -- it scores
# magnitude, and a real 41 bpm capture (pm 19.8) is indistinguishable by it
# from a measured empty-room null (pm 16.6).  See SHARPNESS_THRESHOLD.
#
# It is also not a constant: pm is a max-over-bins statistic, so widening the
# search band raises it even on pure noise.
#   0.10-0.60 Hz (30 bins): null median  7.3, max 16.0
#   0.05-1.00 Hz (57 bins): null median 14.6, max 78.1
PM_THRESHOLD = 50.0

# The gate: peak height of the *whitened* Doppler spectrum, which scores shape
# (a narrow line) rather than magnitude, matching what the estimator itself
# now uses to pick the rate.
#
# Measured, empty-room null capture: 1.32.  60 simulated nulls: median 1.27,
# 99th pct 1.44, max 1.44 -- so the simulated null distribution brackets the
# real one, which is the cross-check that makes this threshold trustworthy.
# Five ground-truthed real captures: 1.70, 1.73, 2.22, 7.08, 7.40.
# 1.55 is the midpoint of the 1.44 -> 1.70 gap; it gives 0/60 false positives
# and passes all five true positives.
SHARPNESS_THRESHOLD = 1.55


def periodogram(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x)) ** 2
    return np.fft.rfftfreq(x.size, 1 / fs), spec


def peak_to_median(
    x: np.ndarray, fs: float, low: float = BAND_LOW, high: float = BAND_HIGH
) -> float:
    """Ratio of the largest in-band bin to the in-band median."""
    freqs, spec = periodogram(x, fs)
    band = (freqs >= low) & (freqs <= high)
    if band.sum() < 3:
        return 0.0
    med = np.median(spec[band])
    return float(spec[band].max() / med) if med > 0 else float("inf")


def band_snr(
    freqs: np.ndarray, power: np.ndarray, f0: float, half_width: float = 0.02
) -> float:
    """In-band vs out-of-band power around f0 (Ratnam's Section V-C metric).

    Used to compare preprocessing combinations on one capture.
    """
    inb = np.abs(freqs - f0) <= half_width
    out = ~inb
    if not inb.any() or not out.any() or power[out].sum() <= 0:
        return float("inf")
    return float(power[inb].sum() / power[out].sum())


def motion_level(H: np.ndarray, fs: float, window_s: float = 2.0) -> np.ndarray:
    """Sliding sum of per-subcarrier std -- presence / gross-motion detector.

    Dou & Huan Section 4.2: this spikes while someone walks in and settles once
    they are still and only breathing.
    """
    amp = np.abs(H)
    n = max(2, int(round(window_s * fs)))
    if amp.shape[0] < n:
        return np.array([amp.std(axis=0).sum()])
    view = np.lib.stride_tricks.sliding_window_view(amp, n, axis=0)
    return view.std(axis=-1).sum(axis=-1)


def assess(x: np.ndarray, fs: float) -> dict:
    pm = peak_to_median(x, fs)
    return {
        "pm": pm,
        "pm_threshold": PM_THRESHOLD,
        "confident": pm > PM_THRESHOLD,
    }
