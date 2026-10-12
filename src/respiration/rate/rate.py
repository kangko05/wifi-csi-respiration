"""Breathing rate of one window of packets.

Method: on the sanitised 10 Hz grid take the power spectrum of every subcarrier, scale
each to unit total power and average them over subcarriers. A breath shows as one
narrow line between 0.1 and 0.5 Hz (6 to 30 breaths per minute). The line must
- stand at least min_peak above the spectrum floor, both the floor of the whole
  0.05-1 Hz band and the floor right around the line, and
- be there in both halves of the window too (a steady period, not one passing swing).
This is done for the amplitude and for the phase; the clearer line gives the rate.
Without such a line the rate is unknown. A line much lower than the window's highest
one never gives the rate, so an unsteady strong period is 'unknown', not replaced by
a weak steady one.

Why a spectrum line and not an autocorrelation peak: the autocorrelation height is the
share of the whole 0-5 Hz power that the period carries, and on real recordings
breathing is a small share (a sleeper's steady 13-16 /min line stood 9-16 times above
the floor while the autocorrelation peak stayed at 0.01-0.10). The line-to-floor ratio
does not depend on how much other power the window holds.

Averaging per-subcarrier spectra is sign free, so subcarriers on which breathing raises
or lowers the amplitude add up instead of cancelling.

What this cannot tell apart: anything else that moves with a steady period in the
band. A machine in the living room gave a 25-28 /min line with nobody there, so a rate
is evidence of a periodic motion, not proof of a breath.

A 20 s breath hold inside the 120 s window does not remove the line (the other 100 s
carry it): at the moment the apnea rule fires, this judge still said RATE in 37 of 38
desk holds and in every lying-down hold at home. That is what makes it usable as the
"there was breathing just before" check on an apnea event.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import signal

from respiration.frame import Frame

from respiration.rate.grid import RATE_HZ, WINDOW_OK, sanitised_grid

RATE = "rate"
UNKNOWN = "unknown"

# Reasons for UNKNOWN besides the grid hold reasons (frozen, lossy, short).
WEAK_PEAK = "weak_peak"
NO_PEAK = "no_peak"
UNSTEADY_PEAK = "unsteady_peak"

AMPLITUDE = "amplitude"
PHASE = "phase"

SECONDS_PER_MINUTE = 60.0

# The spectrum is zero-padded to this many times the window length, so a line is read
# on a grid about four times finer than the window's own resolution.
SPECTRUM_PADDING = 4

# The limits below are in resolution cells: one cell is 1 / window length in Hz
# (0.5 /min for a 120 s window).
# A line this close outside the rate range still counts as inside, so a breath right
# at 6 or 30 /min is not lost to where its peak happens to fall on the grid.
EDGE_TOLERANCE_CELLS = 0.5

# A line outside the range has side lobes inside it. With the Hann window the first
# one is 2.5 cells from the line and about 1/1400 of its power, and they fall with
# the sixth power of the distance (18 dB per octave). An in-range line no higher
# than SIDELOBE_SAFETY times that level of some out-of-range line is dropped: it is
# that line's side lobe, not a rate of its own.
SIDELOBE_FIRST_CELLS = 2.5
SIDELOBE_FIRST_POWER = 1.0 / 1400.0
SIDELOBE_FALL_EXPONENT = 6.0
SIDELOBE_SAFETY = 10.0

# Only lines at least this share of the highest line can give the rate. When the
# highest line is not steady, a much lower one is not a better answer: breathing that
# stopped half way through the window left its second harmonic passing as a steady
# 29 /min line a tenth as high.
DOMINANT_LINE_SHARE = 0.5


@dataclass(frozen=True)
class BreathingConfig:
    """Rate range and how clear a line must be.

    min_lag_s / max_lag_s: shortest and longest breathing period searched, in seconds
        (2 s = 30 /min, 10 s = 6 /min).
    min_peak: lowest line height (a ratio, 1 = no line) for a rate. Empty-room
        windows without a machine stayed below 2.0 and a sleeping person's line was
        6-16, so 5 leaves both sides room. At 4 a random walk without any period got
        a rate in 4 of 60 synthetic windows, at 5 in 2.
    min_half_peak: lowest height of the same line in each half of the window. A half
        window has half the resolution and a rougher floor, so the limit is lower.
    half_tolerance_per_min: how far the line of a half window may sit from the full
        window's line and still count as the same one.
    floor_low_hz / floor_high_hz: band whose median power is the wide spectrum floor.
    local_span: the local floor is the median power between line / local_span and
        line * local_span, without local_guard_hz on each side of the line itself.
        Slow body movement piles power up at the low end of the band; its bumps
        stand high over the wide floor but only 2-4 over their own neighbourhood,
        while a steady breath stood 4-9 over it.
    """

    min_lag_s: float = 2.0
    max_lag_s: float = 10.0
    min_peak: float = 5.0
    min_half_peak: float = 2.5
    half_tolerance_per_min: float = 1.5
    floor_low_hz: float = 0.05
    floor_high_hz: float = 1.0
    local_span: float = 1.7
    local_guard_hz: float = 0.025


@dataclass(frozen=True)
class SpectrumLine:
    """One local maximum of the subcarrier-mean spectrum.

    rate_per_min: line position in breaths per minute.
    height: line power over the spectrum floor (a ratio): the lower of its height
        over the wide floor and over the local floor around the line.
    """

    rate_per_min: float
    height: float


@dataclass(frozen=True)
class BreathingRate:
    """Rate estimate of one window.

    state: RATE or UNKNOWN.
    reason: why UNKNOWN; a grid hold reason (frozen, lossy, short), WEAK_PEAK (best
        line below min_peak), UNSTEADY_PEAK (a line high enough, but not in both
        halves of the window) or NO_PEAK (no line in the range). None for RATE.
    rate_per_min: breaths per minute, set only for RATE.
    peak: height of the chosen line over the spectrum floor (a ratio, 1 = no line),
        set whenever a line was found, so a status line can show how clear the
        period was even when it is too weak.
    signal: AMPLITUDE or PHASE, the signal whose line was chosen (None without a line).
    """

    state: str
    reason: str | None = None
    rate_per_min: float | None = None
    peak: float | None = None
    signal: str | None = None


def mean_spectrum(values: np.ndarray, rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Subcarrier-mean power spectrum of a (T, K) real signal.

    Each subcarrier is linearly detrended (so a slow drift is not taken for a long
    period), Hann windowed, and its spectrum scaled to unit total power before the
    average, so a strong subcarrier counts as much as a weak one.
    Returns (frequency in Hz, mean power), both (F,).
    """
    sample_count = values.shape[0]
    fft_length = 1 << int(np.ceil(np.log2(SPECTRUM_PADDING * sample_count)))

    detrended = signal.detrend(values, axis=0)
    windowed = detrended * signal.windows.hann(sample_count)[:, None]

    power = np.abs(np.fft.rfft(windowed, n=fft_length, axis=0)) ** 2
    power = power / (power.sum(axis=0, keepdims=True) + 1e-30)

    frequency_hz = np.fft.rfftfreq(fft_length, 1.0 / rate_hz)

    return frequency_hz, power.mean(axis=1)


def sidelobe_level(
    line_hz: np.ndarray, line_power: np.ndarray, at_hz: float, cell_hz: float
) -> float:
    """Highest side-lobe power the given lines can have at frequency at_hz.

    line_hz / line_power: (L,) positions and powers of the lines; an empty set gives 0.
    cell_hz: one resolution cell (1 / window length).
    """
    if len(line_hz) == 0:
        return 0.0

    distance_cells = np.maximum(np.abs(line_hz - at_hz) / cell_hz, SIDELOBE_FIRST_CELLS)
    relative_level = (
        SIDELOBE_FIRST_POWER
        * (SIDELOBE_FIRST_CELLS / distance_cells) ** SIDELOBE_FALL_EXPONENT
    )

    return float(np.max(line_power * relative_level))


def spectrum_lines(
    values: np.ndarray, config: BreathingConfig, rate_hz: float
) -> list[SpectrumLine]:
    """Local maxima of the mean spectrum inside the breathing range, highest first.

    A sample is a line when it is above its lower neighbour and not below its upper
    one (a flat top counts once), so a slope that keeps rising to the edge of the
    range is not a line. The range is widened by EDGE_TOLERANCE_CELLS on both sides,
    and side lobes of a stronger line outside it are left out (sidelobe_level).
    """
    frequency_hz, power = mean_spectrum(values, rate_hz)

    in_floor_band = (frequency_hz >= config.floor_low_hz) & (
        frequency_hz <= config.floor_high_hz
    )
    floor = float(np.median(power[in_floor_band])) + 1e-30

    cell_hz = rate_hz / values.shape[0]
    low_hz = 1.0 / config.max_lag_s - EDGE_TOLERANCE_CELLS * cell_hz
    high_hz = 1.0 / config.min_lag_s + EDGE_TOLERANCE_CELLS * cell_hz

    # Local maxima among the samples that have both neighbours (not the first or the
    # last one, which matters when the range reaches the Nyquist frequency).
    inner = np.arange(1, len(power) - 1)
    rises_into = power[inner] > power[inner - 1]
    falls_after = power[inner] >= power[inner + 1]
    peak_indices = inner[rises_into & falls_after]
    peak_hz = frequency_hz[peak_indices]

    is_in_range = (peak_hz >= low_hz) & (peak_hz <= high_hz)
    outside_hz = peak_hz[~is_in_range]
    outside_power = power[peak_indices[~is_in_range]]

    lines = []

    for index in peak_indices[is_in_range]:
        line_hz = frequency_hz[index]

        if power[index] <= SIDELOBE_SAFETY * sidelobe_level(
            outside_hz, outside_power, line_hz, cell_hz
        ):
            continue

        is_near = (frequency_hz >= line_hz / config.local_span) & (
            frequency_hz <= line_hz * config.local_span
        )
        is_near &= np.abs(frequency_hz - line_hz) > config.local_guard_hz
        local_floor = float(np.median(power[is_near])) + 1e-30

        height = min(power[index] / floor, power[index] / local_floor)

        lines.append(
            SpectrumLine(
                rate_per_min=float(line_hz * SECONDS_PER_MINUTE), height=float(height)
            )
        )

    lines.sort(key=lambda line: line.height, reverse=True)

    return lines


def is_steady(
    values: np.ndarray, line: SpectrumLine, config: BreathingConfig, rate_hz: float
) -> bool:
    """True when both halves of the window hold the same line, at least min_half_peak high."""
    half = values.shape[0] // 2

    for part in (values[:half], values[half:]):
        found = False

        for half_line in spectrum_lines(part, config, rate_hz):
            is_same_line = (
                abs(half_line.rate_per_min - line.rate_per_min)
                <= config.half_tolerance_per_min
            )

            if is_same_line and half_line.height >= config.min_half_peak:
                found = True
                break

        if not found:
            return False

    return True


@dataclass(frozen=True)
class SignalLine:
    """Best line of one signal (amplitude or phase) and whether it passed both checks."""

    line: SpectrumLine
    steady: bool


def best_signal_line(
    values: np.ndarray, config: BreathingConfig, rate_hz: float
) -> SignalLine | None:
    """The highest steady line that reaches min_peak, else the highest line as evidence.

    Only lines close to the highest one in height are tried (DOMINANT_LINE_SHARE).
    """
    lines = spectrum_lines(values, config, rate_hz)

    if not lines:
        return None

    lowest_height = max(config.min_peak, DOMINANT_LINE_SHARE * lines[0].height)

    for line in lines:
        if line.height < lowest_height:
            break

        if is_steady(values, line, config, rate_hz):
            return SignalLine(line=line, steady=True)

    return SignalLine(line=lines[0], steady=False)


def grid_breathing_rate(
    grid: np.ndarray,
    config: BreathingConfig = BreathingConfig(),
    rate_hz: float = RATE_HZ,
) -> BreathingRate:
    """Rate from a sanitised grid: (T, K) complex at rate_hz, T samples by K subcarriers.

    The phase is unwrapped along time. A grid shorter than two of the longest periods
    per half window cannot show a steady line and gives NO_PEAK. The 120 s window has
    six of the longest periods per half.
    """
    min_samples = int(round(4 * config.max_lag_s * rate_hz))

    if grid.shape[0] < min_samples:
        return BreathingRate(state=UNKNOWN, reason=NO_PEAK)

    signals = (
        (AMPLITUDE, np.abs(grid)),
        (PHASE, np.unwrap(np.angle(grid), axis=0)),
    )

    found = {}

    for name, values in signals:
        signal_line = best_signal_line(values, config, rate_hz)

        if signal_line is not None:
            found[name] = signal_line

    if not found:
        return BreathingRate(state=UNKNOWN, reason=NO_PEAK)

    # The clearest line of the window, steady or not. Ties go to the amplitude: dict
    # order is AMPLITUDE first and max keeps the first.
    strongest_signal = max(found, key=lambda name: found[name].line.height)
    strongest = found[strongest_signal].line

    if strongest.height < config.min_peak:
        return BreathingRate(
            state=UNKNOWN,
            reason=WEAK_PEAK,
            peak=strongest.height,
            signal=strongest_signal,
        )

    # The rate comes from a steady line close to the clearest one in height. A weak
    # steady line in one signal does not stand in for a strong unsteady one in the other.
    lowest_height = max(config.min_peak, DOMINANT_LINE_SHARE * strongest.height)

    steady_signals = []

    for name, signal_line in found.items():
        if signal_line.steady and signal_line.line.height >= lowest_height:
            steady_signals.append(name)

    if not steady_signals:
        return BreathingRate(
            state=UNKNOWN,
            reason=UNSTEADY_PEAK,
            peak=strongest.height,
            signal=strongest_signal,
        )

    best_signal = max(steady_signals, key=lambda name: found[name].line.height)
    best = found[best_signal].line

    return BreathingRate(
        state=RATE, rate_per_min=best.rate_per_min, peak=best.height, signal=best_signal
    )


def breathing_rate(
    frames: Sequence[Frame],
    window_s: float,
    config: BreathingConfig = BreathingConfig(),
) -> BreathingRate:
    """One window of frames -> breathing rate, or UNKNOWN with the reason.

    window_s is the window length the frames were cut for; the grid needs a continuous
    stretch of at least 83% of it. A window the grid would hold (frozen, lossy, short)
    gives UNKNOWN with that hold reason.
    """
    state, grid = sanitised_grid(frames, window_s)

    if state != WINDOW_OK:
        return BreathingRate(state=UNKNOWN, reason=state)

    return grid_breathing_rate(grid, config)
