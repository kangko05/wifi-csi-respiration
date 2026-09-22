"""Doppler spectrum of the cleaned complex CSI.

Coherent integration across packets per subcarrier, then power summed across
subcarriers.  This is what the phase correction exists to make possible: with
i.i.d. per-packet phase errors left in, the inner sum decoheres and the peak
disappears.

Reference: Ratnam et al. eq (30).
"""

from __future__ import annotations

import numpy as np

from ..preprocess.filters import BAND_HIGH, BAND_LOW

FREQ_STEP = 0.005


def frequency_grid(low: float = BAND_LOW, high: float = BAND_HIGH,
                   step: float = FREQ_STEP) -> np.ndarray:
    return np.arange(low, high + step / 2, step)


def spectrum(H: np.ndarray, fs: float, freqs: np.ndarray | None = None,
             remove_static: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Ratnam eq (30): H(v) = sum_k |sum_p h[p,k] exp(-j2*pi*v*p/fs)|^2.

    Evaluated on an arbitrary frequency grid rather than FFT bins, so the
    resolution in the narrow breathing band does not depend on capture length.
    """
    if freqs is None:
        freqs = frequency_grid()

    if remove_static:
        H = H - H.mean(axis=0, keepdims=True)

    p = H.shape[0]
    t = np.arange(p) / fs
    # [n_freq, P] @ [P, K] -> [n_freq, K], then power-sum over subcarriers.
    basis = np.exp(-2j * np.pi * np.outer(freqs, t))
    return freqs, np.abs(basis @ H).__pow__(2).sum(axis=1)


def stft_energy(x: np.ndarray, fs: float, window_s: float = 1.0,
                hop_s: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """Dou & Huan Section 4.4: zero-frequency STFT energy over time.

    Doppler shift appears only while the chest is actually moving, so the
    accumulated energy rises and falls once per breath -- one peak per cycle,
    even when the amplitude waveform itself is ambiguous.
    """
    n_win = max(4, int(round(window_s * fs)))
    n_hop = max(1, int(round(hop_s * fs)))
    if x.shape[-1] < n_win:
        raise ValueError(f"signal shorter ({x.shape[-1]}) than window ({n_win})")

    window = np.hanning(n_win)
    starts = np.arange(0, x.shape[-1] - n_win + 1, n_hop)
    energy = np.empty(starts.size)
    for i, s in enumerate(starts):
        seg = x[..., s:s + n_win] * window
        energy[i] = np.abs(seg.sum(axis=-1)).__pow__(2).sum()

    # The DC term is constant in time; subtracting the floor exposes the swing.
    return starts / fs, energy - energy.min()
