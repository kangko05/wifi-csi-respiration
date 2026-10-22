"""Per stream and per step: the amplitude variability and the Breathing line score of the
window that ends on that step. A window holds only records stamped before its end."""

import math
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace

import numpy as np

from breathing.scoring import JudgeConfig, WindowScore, score_window, uniform_grid
from breathing.sources import CsiRecord


@dataclass(frozen=True)
class StreamWindow:
    end: float
    motion: float  # median over subcarriers of std / mean of the 1 s mean amplitude, as if no packet were lost
    score: WindowScore
    # The same variability over only the last `recent_s` of the window. The full window keeps a
    # room MOTION for a minute after the person walked out of it; this says where they are now.
    recent_motion: float = 0.0


def variability(times: np.ndarray, amplitude: np.ndarray, start: float, config: JudgeConfig,
                rate_hz: float) -> float:
    """std / mean of the per-bin mean amplitude, median over subcarriers, with the packet-noise
    part of the variance put at its full-rate size.

    Packet noise adds the within-bin variance over the packet count to each bin mean, so the
    plain std / mean rises when packets are lost: 14 % under the 25 % loss the living Case link
    had on the 10-19 night, over the empty-home threshold with nobody home. Swapping that part
    for what `config.rate_hz` packets per bin would give leaves a full-rate window untouched,
    so the thresholds and the night-long room accuracy of the plain feature stay as they were.
    Two variants were worse: the excess std (noise subtracted) clips to 0 on a quiet board and
    leaves no room for a threshold, and the variance ratio (1 = still) doubled the next-room
    leak into the bedroom link."""
    active = amplitude.mean(axis=0) >= config.min_mean_amplitude
    values = amplitude[:, active].astype(np.float64)
    n = int(round(config.window_s * rate_hz))
    index = np.floor((times - start) * rate_hz + 1e-6).astype(int)
    inside = (index >= 0) & (index < n)
    index, values = index[inside], values[inside]
    bin_starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
    counts = np.diff(np.r_[bin_starts, len(index)])
    full = counts >= 2
    bin_starts, counts = bin_starts[full], counts[full]

    if len(bin_starts) < 2:
        return 0.0

    sums = np.add.reduceat(values, bin_starts, axis=0)
    squares = np.add.reduceat(values ** 2, bin_starts, axis=0)
    means = sums / counts[:, None]
    within = (squares - counts[:, None] * means ** 2) / (counts[:, None] - 1)
    nominal_count = config.rate_hz / rate_hz
    sampling = (within / counts[:, None]).mean(axis=0)
    nominal_sampling = within.mean(axis=0) / nominal_count
    variance = np.maximum(means.var(axis=0, ddof=1) - sampling + nominal_sampling, 0.0)
    return float(np.median(np.sqrt(variance) / (means.mean(axis=0) + 1e-9)))


def stream_windows(records: Iterable[CsiRecord], config: JudgeConfig, motion_rate_hz: float,
                   recent_s: float) -> Iterator[StreamWindow]:
    """One window per step boundary crossed; a gap of several steps yields only the latest,
    and a window emptier than `min_fill` yields nothing."""
    times: deque[float] = deque()
    amplitudes: deque[np.ndarray] = deque()
    boundary: float | None = None

    for record in records:
        step_boundary = math.floor(record.time / config.step_s) * config.step_s

        if boundary is None:
            boundary = step_boundary

        if step_boundary > boundary:
            boundary = step_boundary
            start = boundary - config.window_s

            while times and times[0] < start:
                times.popleft()
                amplitudes.popleft()

            if times:
                t = np.fromiter(times, dtype=np.float64, count=len(times))
                a = np.stack(amplitudes)
                score = score_window(t, a, boundary, config)

                if score.fill >= config.min_fill:
                    recent = variability(t, a, boundary - recent_s, replace(config, window_s=recent_s), motion_rate_hz)
                    yield StreamWindow(boundary, variability(t, a, start, config, motion_rate_hz), score, recent)

        times.append(record.time)
        amplitudes.append(np.abs(record.h))
