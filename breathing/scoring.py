"""Breathing line scoring per window and the Quiet baseline threshold.

A window is `window_s` of records ending on a `step_s` boundary of the wall clock. Per
subcarrier the amplitude is bin-averaged onto a `rate_hz` grid (empty bins forward-filled),
z-scored, Hann-tapered and turned into a power spectrum; the spectra are averaged over the
subcarriers. The score is the strongest bin inside the breathing band divided by the median
bin inside the search band. This is the metric that separated the 10-16 doll windows from
the empty room (ADR 0001).
"""

import json
import math
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from breathing.sources import CsiRecord


@dataclass(frozen=True)
class JudgeConfig:
    """Every number the judge uses. Tune here, never inline."""

    window_s: float = 60.0
    step_s: float = 10.0
    rate_hz: float = 100.0
    search_band_hz: tuple[float, float] = (0.08, 0.8)
    breathing_band_rpm: tuple[float, float] = (8.0, 24.0)  # 24 not 30: a 26-28/min periodic signal shows up in empty rooms (10-18 and 10-19 nights)
    min_mean_amplitude: float = 1.0  # subcarriers quieter than this are dropped from the spectrum
    min_fill: float = 0.6  # a window whose grid is emptier than this is not scored (0.6 not 0.9: the bedroom main path dropped to 75% delivery on 10-19 23:20 and the judge must keep scoring)
    baseline_k: float = 3.0  # threshold = median + k * MAD of the Quiet baseline scores
    baseline_min_ratio: float = 1.5  # and never below this multiple of the median (a stationary room has a tiny MAD)
    confirm_windows: int = 2  # consecutive line windows that confirm BREATHING
    apnea_gap_s: float = 15.0  # no breath peak for this long ends BREATHING
    line_lost_windows: int = 9  # BREATHING with no Breathing line for this many consecutive windows is also APNEA (slow path, 90 s)
    waveform_band_hz: tuple[float, float] = (0.1, 0.6)
    waveform_rate_hz: float = 20.0
    waveform_sections: int = 2  # cascaded 2nd-order Butterworth sections per band edge
    # A breath peak is at least this fraction of the segment's median peak. 0.6 not 0.4: under a blanket
    # (10-19 night 02:48-02:57) the median peak fell to 0.09-0.17 and noise peaks of 0.4-0.76x the median
    # kept counting as breaths through three 25-30 s breath holds. Replay 23:41-03:05: holds caught
    # 10/13 -> 12/13, false APNEA shown in bed after the page gate 2 -> 5 in 105 min; 0.8 breaks the doll.
    peak_ratio: float = 0.6
    input_stale_s: float = 5.0  # no record for this long on a live source is "no input"
    motion_band_hz: tuple[float, float] = (0.8, 5.0)
    motion_gate_ratio: float = 2.0

    @property
    def min_breath_interval_s(self) -> float:
        return 60.0 / self.breathing_band_rpm[1]


@dataclass(frozen=True)
class WindowScore:
    end: float
    score: float
    peak_rpm: float
    active_bins: int
    fill: float  # fraction of grid bins that held a record
    motion: float = 0.0


@dataclass(frozen=True)
class Window:
    end: float
    score: WindowScore | None
    note: str  # why there is no score: "warming" or "no input"


def uniform_grid(times: np.ndarray, values: np.ndarray, start: float, duration_s: float,
                 rate_hz: float) -> tuple[np.ndarray, float]:
    """Bin-average onto a uniform grid; empty bins take the previous filled bin."""
    n = int(round(duration_s * rate_hz))
    index = np.floor((times - start) * rate_hz + 1e-6).astype(int)  # a record on a bin edge belongs to that bin
    inside = (index >= 0) & (index < n)
    index = index[inside]
    v = values[inside].astype(np.float64)
    bin_starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
    bin_lengths = np.diff(np.r_[bin_starts, len(index)])
    means = np.add.reduceat(v, bin_starts, axis=0) / bin_lengths[:, None]
    filled = index[bin_starts]
    source = np.maximum(np.searchsorted(filled, np.arange(n), side="right") - 1, 0)
    return means[source], len(filled) / n


def score_window(times: np.ndarray, amplitude: np.ndarray, end: float, config: JudgeConfig) -> WindowScore:
    """`amplitude` is records x subcarriers for the records inside [end - window_s, end)."""
    active = amplitude.mean(axis=0) >= config.min_mean_amplitude
    grid, fill = uniform_grid(times, amplitude[:, active], end - config.window_s, config.window_s, config.rate_hz)
    z = (grid - grid.mean(axis=0)) / (grid.std(axis=0) + 1e-9)
    z = z - z.mean(axis=0)
    taper = np.hanning(len(z))[:, None]
    power = (np.abs(np.fft.rfft(z * taper, axis=0)) ** 2).mean(axis=1)
    rpm = np.fft.rfftfreq(len(z), d=1.0 / config.rate_hz) * 60.0

    low, high = config.search_band_hz
    search = (rpm >= low * 60.0) & (rpm <= high * 60.0)
    band = search & (rpm >= config.breathing_band_rpm[0]) & (rpm <= config.breathing_band_rpm[1])
    noise = np.median(power[search])
    score = power[band].max() / noise if noise > 0.0 else 0.0  # a flat window scores zero
    peak_rpm = rpm[search][np.argmax(power[search])]
    return WindowScore(end=end, score=float(score), peak_rpm=float(peak_rpm), active_bins=int(active.sum()), fill=fill,
                       motion=motion_rms(grid, config))


def motion_rms(grid: np.ndarray, config: JudgeConfig) -> float:
    """Relative-amplitude RMS (%) of the window inside motion_band_hz, averaged over subcarriers."""
    rel = grid / (grid.mean(axis=0) + 1e-9) - 1.0
    spec = np.fft.rfft(rel * np.hanning(len(rel))[:, None], axis=0)
    hz = np.fft.rfftfreq(len(rel), d=1.0 / config.rate_hz)
    low, high = config.motion_band_hz
    spec[(hz < low) | (hz >= high)] = 0.0
    return float(100.0 * np.sqrt((np.fft.irfft(spec, n=len(rel), axis=0) ** 2).mean()))


class Windower:
    """Buffers (time, amplitude) and scores one window at every step_s boundary crossed."""

    def __init__(self, config: JudgeConfig) -> None:
        self._config = config
        self._times: deque[float] = deque()
        self._amplitudes: deque[np.ndarray] = deque()
        self._boundary: float | None = None
        self._first_time: float | None = None

    def feed(self, time: float, amplitude: np.ndarray) -> Window | None:
        step = self._config.step_s
        boundary = math.floor(time / step) * step

        if self._boundary is None:
            self._boundary = boundary
            self._first_time = time

        self._times.append(time)
        self._amplitudes.append(amplitude)

        if boundary <= self._boundary:
            return None

        self._boundary = boundary
        start = boundary - self._config.window_s

        while self._times and self._times[0] < start:
            self._times.popleft()
            self._amplitudes.popleft()

        inside = sum(1 for t in self._times if t < boundary)
        note = "warming" if self._first_time > start else "no input"

        if inside == 0:
            return Window(boundary, None, note)

        times = np.fromiter(self._times, dtype=np.float64, count=len(self._times))[:inside]
        amplitude = np.stack([self._amplitudes[i] for i in range(inside)])
        score = score_window(times, amplitude, boundary, self._config)

        if score.fill < self._config.min_fill:
            return Window(boundary, None, note)

        return Window(boundary, score, "")


def window_scores(records: Iterable[CsiRecord], config: JudgeConfig) -> Iterator[WindowScore]:
    """The score of every scorable window of a record stream, in order."""
    windower = Windower(config)

    for record in records:
        window = windower.feed(record.time, np.abs(record.h))

        if window is not None and window.score is not None:
            yield window.score


@dataclass(frozen=True)
class QuietBaseline:
    """Window scores recorded at the judge's place with no breathing source; the threshold
    is median + k * MAD so one noisy window does not set it."""

    scores: tuple[float, ...]
    k: float
    min_ratio: float
    motions: tuple[float, ...] = ()

    @property
    def motion_level(self) -> float | None:
        return float(np.median(self.motions)) if self.motions else None

    def motion_threshold(self, config: JudgeConfig) -> float:
        """Windows above this are motion. Infinite when the gate is off or the baseline has no motion levels."""
        if config.motion_gate_ratio <= 0.0 or self.motion_level is None:
            return math.inf

        return config.motion_gate_ratio * self.motion_level

    @property
    def median(self) -> float:
        return float(np.median(self.scores))

    @property
    def mad(self) -> float:
        return float(np.median(np.abs(np.array(self.scores) - self.median)))

    @property
    def threshold(self) -> float:
        return max(self.median + self.k * self.mad, self.min_ratio * self.median)

    @classmethod
    def from_scores(cls, scores: Iterable[float], config: JudgeConfig) -> "QuietBaseline":
        scores = tuple(scores)

        if not scores:
            raise ValueError(f"baseline needs more than {config.window_s:.0f} s of records")

        return cls(scores=scores, k=config.baseline_k, min_ratio=config.baseline_min_ratio)

    @classmethod
    def from_windows(cls, windows: Iterable[WindowScore], config: JudgeConfig) -> "QuietBaseline":
        windows = tuple(windows)
        quiet = cls.from_scores((w.score for w in windows), config)
        return cls(scores=quiet.scores, k=quiet.k, min_ratio=quiet.min_ratio, motions=tuple(w.motion for w in windows))

    @classmethod
    def from_records(cls, records: Iterable[CsiRecord], config: JudgeConfig) -> "QuietBaseline":
        return cls.from_windows(window_scores(records, config), config)

    def to_json(self) -> str:
        return json.dumps({"k": self.k, "min_ratio": self.min_ratio, "median": self.median, "mad": self.mad,
                           "threshold": self.threshold, "scores": list(self.scores), "motions": list(self.motions)})

    @classmethod
    def from_json(cls, text: str) -> "QuietBaseline":
        data = json.loads(text)
        return cls(scores=tuple(float(s) for s in data["scores"]), k=float(data["k"]), min_ratio=float(data["min_ratio"]),
                   motions=tuple(float(m) for m in data.get("motions", ())))
