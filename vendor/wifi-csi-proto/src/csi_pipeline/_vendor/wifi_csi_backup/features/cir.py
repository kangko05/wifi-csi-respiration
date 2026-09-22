"""Multipath decomposition via the channel impulse response.

The IFFT over subcarriers separates paths by delay.  Chest reflections land in
the first few taps, so keeping those and discarding the rest removes most of
the static multipath clutter.

Reference: Dou & Huan, "Full Respiration Rate Monitoring Exploiting Doppler
Information with Commodity Wi-Fi Devices", Sensors 2021, Section 3.3.2.
"""

from __future__ import annotations

import numpy as np

from ..model import CsiBlock


def remove_static(H: np.ndarray) -> np.ndarray:
    """Subtract the per-subcarrier time average (the static component b_k).

    What remains is the dynamic component; the static part is orders of
    magnitude larger and would otherwise bury the breathing signal.
    """
    return H - H.mean(axis=0, keepdims=True)


def to_cir(H: np.ndarray, n_taps: int | None = None) -> np.ndarray:
    """IFFT along the subcarrier axis: [P, K] CSI -> [P, n_taps] CIR."""
    cir = np.fft.ifft(H, axis=-1)
    return cir if n_taps is None else cir[..., :n_taps]


def dynamic_taps(block: CsiBlock, n_taps: int = 3,
                 static_removed: bool = False) -> np.ndarray:
    """Return the leading CIR taps that carry the dynamic component."""
    H = block.H if static_removed else remove_static(block.H)
    return to_cir(H, n_taps)


def tap_delays(n_taps: int, spacing: float = 312_500.0,
               n_subcarriers: int = 114) -> np.ndarray:
    """Delay in seconds of each CIR tap, for sanity-checking the tap choice."""
    return np.arange(n_taps) / (spacing * n_subcarriers)
