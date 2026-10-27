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
from dataclasses import dataclass, replace

import numpy as np

from breathing.sources import CsiRecord


@dataclass(frozen=True)
class JudgeConfig:
    """Every number the judge uses. Tune here, never inline."""

    window_s: float = 60.0
    step_s: float = 10.0
    rate_hz: float = 100.0
    search_band_hz: tuple[float, float] = (0.08, 0.8)
    # The doll demo band (demo.py and demo_ui.py pass it too): the lab room showed a 23-24/min floor line on 10-21.
    # For a sleeper the band was 8-24 (24 not 30: a 26-28/min periodic signal shows up in empty rooms, 10-18/10-19).
    breathing_band_rpm: tuple[float, float] = (10.0, 20.0)
    min_mean_amplitude: float = 1.0  # subcarriers quieter than this are dropped from the spectrum
    min_fill: float = 0.6  # a window whose grid is emptier than this is not scored (0.6 not 0.9: the bedroom main path dropped to 75% delivery on 10-19 23:20 and the judge must keep scoring)
    baseline_k: float = 3.0  # threshold = median + k * MAD of the Quiet baseline scores
    baseline_min_ratio: float = 1.5  # and never below this multiple of the median (a stationary room has a tiny MAD)
    # A baseline keeps at least this fraction of its windows after dropping motion and line windows, so a
    # disturbed calibration still yields a baseline and its `excluded` list says why it is wide.
    baseline_keep_ratio: float = 0.5
    confirm_windows: int = 2  # consecutive line windows that confirm BREATHING
    apnea_gap_s: float = 15.0  # no breath peak for this long ends BREATHING
    line_lost_windows: int = 9  # BREATHING with no Breathing line for this many consecutive windows is also APNEA (slow path, 90 s)
    # APNEA also returns to BREATHING on this many consecutive shorter windows scored against their own Quiet
    # baseline. 10-21 doll replay: back to BREATHING 48 s -> 26 s after the doll resumed, pauses caught 9/10 -> 10/10;
    # one 30 s window instead of two returned in 17% of empty-room steps (docs/reports/20261026-return-speed.md).
    return_window_s: float = 30.0
    return_confirm_windows: int = 2
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

    @property
    def return_config(self) -> "JudgeConfig":
        """The same judge numbers on the return window."""
        return replace(self, window_s=self.return_window_s)


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


def calibration_scores(records: Iterable[CsiRecord], config: JudgeConfig) -> Iterator[tuple[bool, WindowScore]]:
    """One pass over a calibration stream scoring both window lengths: (is_return_window, score)."""
    windowers = ((False, Windower(config)), (True, Windower(config.return_config)))

    for record in records:
        amplitude = np.abs(record.h)

        for is_return, windower in windowers:
            window = windower.feed(record.time, amplitude)

            if window is not None and window.score is not None:
                yield is_return, window.score


@dataclass(frozen=True)
class ExcludedWindow:
    """A calibration window the Quiet baseline did not learn from, and why."""

    end: float
    score: float
    motion: float
    reason: str

    def to_dict(self) -> dict:
        return {"end": self.end, "score": self.score, "motion": self.motion, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> "ExcludedWindow":
        return cls(end=float(data["end"]), score=float(data["score"]), motion=float(data["motion"]), reason=str(data["reason"]))


@dataclass(frozen=True)
class QuietBaseline:
    """Window scores recorded at the judge's place with no breathing source; the threshold
    is median + k * MAD so one noisy window does not set it. `from_windows` drops the windows
    that were not quiet (see `clip_baseline_windows`) and lists them in `excluded`. `fast_return`
    is the same calibration scored on `return_window_s` windows; without it the judge leaves APNEA
    on full windows only."""

    scores: tuple[float, ...]
    k: float
    min_ratio: float
    motions: tuple[float, ...] = ()
    excluded: tuple[ExcludedWindow, ...] = ()
    fast_return: "QuietBaseline | None" = None
    window_s: float | None = None  # window length the scores were taken on; None in files written before 10-27

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
    def from_windows(cls, windows: Iterable[WindowScore], config: JudgeConfig,
                     return_windows: Iterable[WindowScore] = ()) -> "QuietBaseline":
        """`return_windows`, scored on `return_window_s` windows, give `fast_return`; none leave it unset."""
        windows = tuple(windows)

        if not windows:
            raise ValueError(f"baseline needs more than {config.window_s:.0f} s of records")

        kept, excluded = clip_baseline_windows(windows, config)
        quiet = cls.from_scores((w.score for w in kept), config)
        return_windows = tuple(return_windows)
        fast_return = cls.from_windows(return_windows, config.return_config) if return_windows else None
        return cls(scores=quiet.scores, k=quiet.k, min_ratio=quiet.min_ratio, motions=tuple(w.motion for w in kept),
                   excluded=excluded, fast_return=fast_return, window_s=config.window_s)

    @classmethod
    def from_records(cls, records: Iterable[CsiRecord], config: JudgeConfig) -> "QuietBaseline":
        windows: dict[bool, list[WindowScore]] = {False: [], True: []}

        for is_return, score in calibration_scores(records, config):
            windows[is_return].append(score)

        return cls.from_windows(windows[False], config, windows[True])

    def to_dict(self) -> dict:
        data = {"k": self.k, "min_ratio": self.min_ratio, "median": self.median, "mad": self.mad,
                "threshold": self.threshold, "scores": list(self.scores), "motions": list(self.motions),
                "excluded": [w.to_dict() for w in self.excluded], "window_s": self.window_s}

        if self.fast_return is not None:
            data["fast_return"] = self.fast_return.to_dict()

        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "QuietBaseline":
        fast_return = data.get("fast_return")
        window_s = data.get("window_s")
        return cls(scores=tuple(float(s) for s in data["scores"]), k=float(data["k"]), min_ratio=float(data["min_ratio"]),
                   motions=tuple(float(m) for m in data.get("motions", ())),
                   excluded=tuple(ExcludedWindow.from_dict(w) for w in data.get("excluded", ())),
                   fast_return=None if fast_return is None else cls.from_dict(fast_return),
                   window_s=None if window_s is None else float(window_s))

    @classmethod
    def from_json(cls, text: str) -> "QuietBaseline":
        return cls.from_dict(json.loads(text))


def clip_baseline_windows(windows: Iterable[WindowScore],
                          config: JudgeConfig) -> tuple[list[WindowScore], tuple[ExcludedWindow, ...]]:
    """Splits calibration windows into the ones a Quiet baseline may learn from and the ones it
    must not.

    Motion first: a window whose motion RMS is above `motion_gate_ratio` x the median motion of
    all windows is dropped, because a moving body stirs the breathing band as well. Then lines:
    a window whose score is above the threshold the remaining windows would give (median + k *
    MAD, floored by min_ratio) is dropped, and this repeats until no kept window would count as
    a line against the baseline it is part of. Dropping stops at `baseline_keep_ratio` of the
    windows; the highest offenders go first."""
    kept = list(windows)
    floor = max(1, math.ceil(config.baseline_keep_ratio * len(kept)))
    excluded: list[ExcludedWindow] = []

    def drop(candidates: list[WindowScore], reason: str) -> None:
        room = len(kept) - floor
        dropped = set(candidates[:room])

        for window in candidates[:room]:
            excluded.append(ExcludedWindow(window.end, window.score, window.motion, reason.format(window=window)))

        kept[:] = [w for w in kept if w not in dropped]

    if config.motion_gate_ratio > 0.0:
        motion_limit = config.motion_gate_ratio * float(np.median([w.motion for w in kept]))
        moving = sorted((w for w in kept if w.motion > motion_limit), key=lambda w: w.motion, reverse=True)
        drop(moving, f"motion {{window.motion:.2f}} > {motion_limit:.2f}")

    while len(kept) > floor:
        quiet = QuietBaseline.from_scores((w.score for w in kept), config)
        lines = sorted((w for w in kept if w.score > quiet.threshold), key=lambda w: w.score, reverse=True)

        if not lines:
            break

        drop(lines, f"line {{window.score:.2f}} > {quiet.threshold:.2f}")

    excluded.sort(key=lambda w: w.end)
    return kept, tuple(excluded)
