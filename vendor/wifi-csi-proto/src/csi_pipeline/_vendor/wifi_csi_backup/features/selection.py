"""Rank subcarriers / CIR taps by how much in-band energy they carry."""

from __future__ import annotations

import numpy as np

from ..preprocess.filters import BAND_HIGH, BAND_LOW


def band_power(x: np.ndarray, fs: float, low: float = BAND_LOW,
               high: float = BAND_HIGH) -> np.ndarray:
    """In-band power of each column of ``x`` ([N, C] real or complex).

    Uses the full FFT so complex columns (CIR taps) count both Doppler signs;
    the chest moves toward and away, so the energy sits on both sides of DC.
    """
    if x.ndim == 1:
        x = x[:, None]
    n = x.shape[0]
    spec = np.abs(np.fft.fft(x - x.mean(axis=0, keepdims=True), axis=0)) ** 2
    freqs = np.fft.fftfreq(n, 1 / fs)
    band = (np.abs(freqs) >= low) & (np.abs(freqs) <= high)
    return spec[band].sum(axis=0)


def rank(x: np.ndarray, fs: float, top: int = 5) -> np.ndarray:
    """Indices of the ``top`` columns with the most in-band energy."""
    power = band_power(x, fs)
    return np.argsort(power)[::-1][:top]


def project(z: np.ndarray) -> np.ndarray:
    """Complex column -> real, along the axis of maximum variance.

    Taking the real part instead would halve the period: chest motion appears
    as ``exp(j*delta*sin(wt))``, whose real part is ``1 - delta^2*sin^2(wt)/2``
    -- pure second harmonic, so the rate comes out doubled.  The principal axis
    keeps the fundamental.
    """
    z = z - z.mean(axis=0, keepdims=True)
    # Angle that maximises Var(Re(z * exp(-j*theta))) for each column.
    theta = 0.5 * np.angle(np.sum(z ** 2, axis=0))
    return np.real(z * np.exp(-1j * theta))


def combine(x: np.ndarray, fs: float, top: int = 5) -> np.ndarray:
    """Sum the best columns after aligning their signs.

    Different subcarriers can respond to the same chest motion with opposite
    polarity (they sit on different sides of a Fresnel boundary), so summing
    them raw partly cancels the signal.
    """
    idx = rank(x, fs, top)
    sel = x[:, idx]
    sel = project(sel) if np.iscomplexobj(sel) else sel - sel.mean(axis=0,
                                                                  keepdims=True)

    ref = sel[:, 0]
    signs = np.sign([np.dot(ref, sel[:, i]) or 1.0 for i in range(sel.shape[1])])
    return (sel * signs).sum(axis=1)
