"""The breathing rate judge on synthetic windows."""

import numpy as np
import pytest

from respiration.frame import Frame
from respiration.rate.grid import HOLD_FROZEN, HOLD_LOSSY, HOLD_SHORT
from respiration.rate.rate import (
    AMPLITUDE,
    PHASE,
    RATE,
    UNKNOWN,
    WEAK_PEAK,
    BreathingConfig,
    breathing_rate,
    grid_breathing_rate,
)
from synthetic_frames import frames

WINDOW_S = 120.0

# 15 /min synthetic breathing must come out within 2 /min.
RATE_TOLERANCE_PER_MIN = 2.0

MIN_PEAK = BreathingConfig().min_peak


def breathing_window(rate_per_min, seed):
    """120 s of synthetic frames breathing at rate_per_min."""
    return frames(WINDOW_S, True, seed=seed, breathing_hz=rate_per_min / 60.0)


def drop_bursts(frame_list):
    """Lose 25% of frames in bursts of 40 (the grid holds as lossy)."""
    kept = []

    for index, frame in enumerate(frame_list):
        if (index // 40) % 4 != 0:
            kept.append(frame)

    return kept


def phase_only_window(rate_per_min, seed, bin_count=30, packet_rate_hz=92.0):
    """Breathing only in the phase: constant amplitude, per-bin phase swing.

    The swing differs per bin (not a straight line across bins), so the CFO/STO removal
    keeps it. A random common phase per packet stands for the CFO.
    """
    rng = np.random.default_rng(seed)
    packet_count = int(WINDOW_S * packet_rate_hz)
    time_s = np.arange(packet_count) / packet_rate_hz

    base = 40 * np.exp(1j * rng.uniform(-np.pi, np.pi, bin_count))
    swing_per_bin = rng.normal(0, 0.3, bin_count)
    breath = np.sin(2 * np.pi * rate_per_min / 60.0 * time_s)
    cfo = rng.uniform(-np.pi, np.pi, packet_count)

    phase = breath[:, None] * swing_per_bin[None, :] + cfo[:, None]
    noise = rng.normal(0, 1.0, (packet_count, bin_count)) + 1j * rng.normal(0, 1.0, (packet_count, bin_count))
    csi = (base[None, :] * np.exp(1j * phase) + noise).astype(np.complex64)

    out = []
    for t_s, row in zip(time_s, csi):
        out.append(Frame(t_s=float(t_s), device_time_us=int(t_s * 1e6), csi=row))

    return out


def clean_grid(rate_per_min, seconds=WINDOW_S, rate_hz=10.0, bin_count=8):
    """Sanitised-grid stand-in: a pure breathing sine, opposite on every other bin, no noise."""
    time_s = np.arange(int(seconds * rate_hz)) / rate_hz
    direction = np.where(np.arange(bin_count) % 2 == 0, 1.0, -1.0)
    breath = 0.05 * np.sin(2 * np.pi * rate_per_min / 60.0 * time_s)

    return (1 + breath[:, None] * direction[None, :]).astype(np.complex128)


def test_fifteen_per_minute_is_found():
    result = breathing_rate(breathing_window(15, seed=4), WINDOW_S)

    assert result.state == RATE
    assert result.rate_per_min == pytest.approx(15, abs=RATE_TOLERANCE_PER_MIN)
    assert result.peak >= MIN_PEAK
    assert result.reason is None


@pytest.mark.parametrize("rate_per_min", [8, 12, 20, 26])
def test_other_rates_in_the_band_are_found(rate_per_min):
    result = breathing_rate(breathing_window(rate_per_min, seed=5), WINDOW_S)

    assert result.state == RATE
    assert result.rate_per_min == pytest.approx(rate_per_min, abs=RATE_TOLERANCE_PER_MIN)


@pytest.mark.parametrize("seed", [3, 6, 7])
def test_noise_only_is_unknown(seed):
    result = breathing_rate(frames(WINDOW_S, False, seed=seed), WINDOW_S)

    assert result.state == UNKNOWN
    assert result.reason == WEAK_PEAK
    assert result.rate_per_min is None
    assert result.peak < MIN_PEAK


def test_amplitude_breathing_uses_the_amplitude():
    # The generator's phase swing is linear across bins, which the STO removal takes
    # out, so its breathing lives in the amplitude.
    result = breathing_rate(breathing_window(15, seed=4), WINDOW_S)

    assert result.signal == AMPLITUDE


def test_phase_only_breathing_uses_the_phase():
    result = breathing_rate(phase_only_window(15, seed=9), WINDOW_S)

    assert result.state == RATE
    assert result.signal == PHASE
    assert result.rate_per_min == pytest.approx(15, abs=RATE_TOLERANCE_PER_MIN)


def test_lossy_window_is_unknown_with_that_reason():
    result = breathing_rate(drop_bursts(frames(130, True, seed=5)), WINDOW_S)

    assert result.state == UNKNOWN
    assert result.reason == HOLD_LOSSY
    assert result.peak is None


def test_frozen_receiver_is_unknown_with_that_reason():
    result = breathing_rate(frames(WINDOW_S, True, seed=5, frozen_span=(1000, 1300)), WINDOW_S)

    assert result.state == UNKNOWN
    assert result.reason == HOLD_FROZEN


def test_too_few_frames_is_short():
    result = breathing_rate(frames(30, True, seed=5), WINDOW_S)

    assert result.state == UNKNOWN
    assert result.reason == HOLD_SHORT
    assert breathing_rate([], WINDOW_S).reason == HOLD_SHORT


@pytest.mark.parametrize("rate_per_min", [6.0, 30.0])
def test_rate_right_at_the_range_edge_is_found(rate_per_min):
    # The peak of an exact 30 /min sine falls a hair outside 0.5 Hz on the spectrum
    # grid; without the edge tolerance its side lobe (28.8 /min) was reported.
    result = grid_breathing_rate(clean_grid(rate_per_min))

    assert result.state == RATE
    assert result.rate_per_min == pytest.approx(rate_per_min, abs=0.25)


@pytest.mark.parametrize("rate_per_min", [3.0, 4.5, 31.5, 33.0, 36.0])
def test_period_outside_the_range_gives_no_rate(rate_per_min):
    # Not even through a side lobe that reaches into the range.
    result = grid_breathing_rate(clean_grid(rate_per_min))

    assert result.state == UNKNOWN
    assert result.rate_per_min is None
