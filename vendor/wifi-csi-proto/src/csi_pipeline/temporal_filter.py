"""Finite-support, centered FIR experiments after anti-alias downsampling.

No padding, gap filling, labels or column selection occurs here. A trim-only
control retains exactly the same timestamps as the two filtered paths.
"""

from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass(frozen=True)
class TemporalFilterConfig:
    kind: str = "bandpass"
    half_width_s: float = 10.0
    low_hz: float = 0.1
    high_hz: float = 0.8
    kaiser_beta: float = 8.0

    def __post_init__(self):
        if self.kind not in ("trim", "bandpass", "lowpass"):
            raise ValueError("kind must be trim, bandpass or lowpass")
        for name in ("half_width_s", "low_hz", "high_hz", "kaiser_beta"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.low_hz >= self.high_hz:
            raise ValueError("low_hz must be below high_hz")


def design_temporal_filter(config: TemporalFilterConfig, sample_rate_hz: float):
    """Return odd symmetric taps and exact half-support in output samples."""
    if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 2 * config.high_hz:
        raise ValueError("sample rate must exceed twice the upper cutoff")
    half = int(np.ceil(config.half_width_s * sample_rate_hz))
    if config.kind == "trim":
        taps = np.zeros(2 * half + 1)
        taps[half] = 1.0
    else:
        cutoff = [config.low_hz, config.high_hz] if config.kind == "bandpass" else config.high_hz
        taps = signal.firwin(2 * half + 1, cutoff, fs=sample_rate_hz,
                            pass_zero=config.kind == "lowpass", window=("kaiser", config.kaiser_beta))
    taps.setflags(write=False)
    return taps, half


def apply_temporal_filter(values, times, sample_rate_hz, config):
    """Filter a supported regular grid; return centered valid output and trim.

    The causal FIR delay is half/sample_rate_hz, compensated by centered timestamp
    assignment. This is offline, requires future samples, and has zero retained
    phase delay. Unsupported short inputs return empty arrays without extrapolation.
    """
    taps, half = design_temporal_filter(config, sample_rate_hz)
    if len(times) <= 2 * half:
        return values[:0].copy(), times[:0].copy(), half / sample_rate_hz
    filtered = (values[half:-half].copy() if config.kind == "trim" else
                signal.convolve(values, taps[:, None], mode="valid", method="direct"))
    return filtered, times[half:-half].copy(), half / sample_rate_hz
