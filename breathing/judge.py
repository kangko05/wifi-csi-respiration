"""The Breathing module's judge: records in, one `Tick` per step and per state change out.

Two rules decide the state. The Breathing line rule works on the spectral window score
against the Quiet baseline threshold and confirms BREATHING after `confirm_windows`
consecutive line windows. The Apnea rule works on the Breathing waveform in the time domain:
once BREATHING is confirmed, `apnea_gap_s` without a breath peak means APNEA. APNEA is left
only by the line rule, never by time.

A live source also calls `heartbeat(now)` while nothing arrives; after `input_stale_s` the
judge reports "no input" with the state unchanged, once per step.
"""

import json
import math
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum

import numpy as np

from breathing.scoring import JudgeConfig, QuietBaseline, Window, Windower, WindowScore
from breathing.sources import CsiRecord


class State(Enum):
    NO_SIGNAL = "NO_SIGNAL"
    BREATHING = "BREATHING"
    APNEA = "APNEA"


@dataclass(frozen=True)
class Tick:
    time: float
    state: State
    score: float | None
    threshold: float | None
    peak_rpm: float | None
    line: bool  # Breathing line present in the window that ended at `time`
    last_breath_time: float | None
    reason: str
    input_ok: bool  # False on a tick raised by input loss rather than by a window

    def to_json(self) -> str:
        return json.dumps({
            "time": round(self.time, 3),
            "state": self.state.value,
            "score": None if self.score is None else round(self.score, 3),
            "threshold": None if self.threshold is None else round(self.threshold, 3),
            "peak_rpm": None if self.peak_rpm is None else round(self.peak_rpm, 2),
            "line": self.line,
            "last_breath_time": None if self.last_breath_time is None else round(self.last_breath_time, 3),
            "reason": self.reason,
            "input_ok": self.input_ok,
        })


def transition(state: State, line_confirmed: bool, gap_exceeded: bool) -> State:
    """The whole state machine. NO_SIGNAL never reaches APNEA, and APNEA never times out."""
    match state, line_confirmed, gap_exceeded:
        case State.NO_SIGNAL, True, _:
            return State.BREATHING
        case State.BREATHING, _, True:
            return State.APNEA
        case State.APNEA, True, _:
            return State.BREATHING
        case _:
            return state


class Biquad:
    """One 2nd-order IIR section, direct form I."""

    def __init__(self, b: tuple[float, float, float], a: tuple[float, float, float]) -> None:
        self._b = tuple(v / a[0] for v in b)
        self._a = tuple(v / a[0] for v in a)
        self._x = [0.0, 0.0]
        self._y = [0.0, 0.0]

    def process(self, x: float) -> float:
        b0, b1, b2 = self._b
        _, a1, a2 = self._a
        y = b0 * x + b1 * self._x[0] + b2 * self._x[1] - a1 * self._y[0] - a2 * self._y[1]
        self._x = [x, self._x[0]]
        self._y = [y, self._y[0]]
        return y

    @classmethod
    def butterworth(cls, kind: str, cutoff_hz: float, rate_hz: float) -> "Biquad":
        """Audio EQ cookbook low-pass or high-pass with the Butterworth Q."""
        w0 = 2.0 * math.pi * cutoff_hz / rate_hz
        alpha = math.sin(w0) / (2.0 * math.sqrt(0.5))
        c = math.cos(w0)

        if kind == "lowpass":
            b = ((1.0 - c) / 2.0, 1.0 - c, (1.0 - c) / 2.0)
        else:
            b = ((1.0 + c) / 2.0, -(1.0 + c), (1.0 + c) / 2.0)

        return cls(b, (1.0 + alpha, -2.0 * c, 1.0 - alpha))


class BreathTracker:
    """The Breathing waveform and the breath peaks on it.

    Amplitudes are bin-averaged onto a `waveform_rate_hz` grid. From the start of a BREATHING
    segment each subcarrier is z-scored with that segment's opening window and its sign is
    aligned to the window's first principal component, so the subcarrier mean adds the
    breathing motion coherently; the mean is band-passed causally. A breath is a positive
    local maximum at least `peak_ratio` of the segment's median peak height and at least one
    shortest breath after the previous one.
    """

    def __init__(self, config: JudgeConfig) -> None:
        self._config = config
        self._cell_s = 1.0 / config.waveform_rate_hz
        self._cells: deque[tuple[float, np.ndarray]] = deque()
        self._cell_index: int | None = None
        self._cell_sum: np.ndarray | None = None
        self._cell_count = 0
        self._mean: np.ndarray | None = None
        self._scale: np.ndarray | None = None
        self._sign: np.ndarray | None = None
        self._filters: list[Biquad] = []
        self._previous = (0.0, 0.0)  # the two latest waveform samples
        self._previous_time: float | None = None
        self._heights: list[float] = []
        self.last_breath_time: float | None = None

    def feed(self, time: float, amplitude: np.ndarray) -> None:
        index = math.floor(time / self._cell_s)

        if self._cell_index is None:
            self._cell_index = index
            self._cell_sum = np.zeros_like(amplitude)

        if index > self._cell_index:
            mean = self._cell_sum / self._cell_count

            for closed in range(self._cell_index, index):  # empty cells repeat the last mean
                self._push((closed + 1) * self._cell_s, mean)

            self._cell_index = index
            self._cell_sum = np.zeros_like(amplitude)
            self._cell_count = 0

        self._cell_sum += amplitude
        self._cell_count += 1

    def start_segment(self, time: float) -> None:
        """Learn the subcarrier weights and the peak reference from the buffered window and
        continue the filter from the end of that window."""
        config = self._config
        times, cells = self._window()
        self._mean, self._scale, self._sign = _fit_projection(cells)
        self._filters = _band_filters(config)
        waveform = np.array([self._filter(self._project(c)) for c in cells])

        settled = times >= times[0] + 2.0 / config.waveform_band_hz[0]  # two periods of the low edge
        peaks = _pick_peaks(times, waveform, settled, config.min_breath_interval_s)
        self._heights = [float(waveform[i]) for i in peaks]
        self.last_breath_time = float(times[max(peaks)]) if peaks else time
        self._previous = (float(waveform[-2]), float(waveform[-1])) if len(waveform) >= 2 else (0.0, 0.0)
        self._previous_time = float(times[-1])

    def window_waveform(self) -> tuple[np.ndarray, np.ndarray]:
        """Display only: the buffered window projected and band-passed from scratch, in any
        state. Returns (cell times, waveform)."""
        if len(self._cells) < 2:
            return np.zeros(0), np.zeros(0)

        times, cells = self._window()
        mean, scale, sign = _fit_projection(cells)
        filters = _band_filters(self._config)
        return times, np.array([_run_filters(filters, _project(c, mean, scale, sign)) for c in cells])

    def _window(self) -> tuple[np.ndarray, np.ndarray]:
        return np.array([t for t, _ in self._cells]), np.stack([c for _, c in self._cells])

    @property
    def breath_threshold(self) -> float:
        return self._config.peak_ratio * float(np.median(self._heights)) if self._heights else 0.0

    def _push(self, cell_time: float, mean: np.ndarray) -> None:
        self._cells.append((cell_time, mean))

        while self._cells[0][0] < cell_time - self._config.window_s:
            self._cells.popleft()

        if self._sign is None:
            return

        y = self._filter(self._project(mean))
        older, latest = self._previous
        latest_time = self._previous_time

        if latest > older and latest >= y and latest > 0.0 and latest >= self.breath_threshold \
                and latest_time - self.last_breath_time >= self._config.min_breath_interval_s:
            self.last_breath_time = latest_time
            self._heights.append(latest)

        self._previous = (latest, y)
        self._previous_time = cell_time

    def _project(self, mean: np.ndarray) -> float:
        return _project(mean, self._mean, self._scale, self._sign)

    def _filter(self, x: float) -> float:
        return _run_filters(self._filters, x)


def _fit_projection(cells: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-subcarrier mean and scale of a window, and the sign that aligns each subcarrier
    with the window's first principal component."""
    mean = cells.mean(axis=0)
    scale = cells.std(axis=0) + 1e-9
    z = (cells - mean) / scale
    principal = np.linalg.eigh(z.T @ z)[1][:, -1]
    return mean, scale, np.where(principal < 0.0, -1.0, 1.0)


def _project(cell: np.ndarray, mean: np.ndarray, scale: np.ndarray, sign: np.ndarray) -> float:
    return float(((cell - mean) / scale) @ sign) / len(cell)


def _band_filters(config: JudgeConfig) -> list[Biquad]:
    return [Biquad.butterworth(kind, cutoff, config.waveform_rate_hz)
            for kind, cutoff in (("highpass", config.waveform_band_hz[0]), ("lowpass", config.waveform_band_hz[1]))
            for _ in range(config.waveform_sections)]


def _run_filters(filters: list[Biquad], x: float) -> float:
    for biquad in filters:
        x = biquad.process(x)

    return x


def _pick_peaks(times: np.ndarray, waveform: np.ndarray, allowed: np.ndarray, min_interval_s: float) -> list[int]:
    """Positive local maxima, tallest first, each at least `min_interval_s` from the others."""
    local = np.flatnonzero((waveform[1:-1] > waveform[:-2]) & (waveform[1:-1] >= waveform[2:])) + 1
    local = [i for i in local if allowed[i] and waveform[i] > 0.0]
    picked: list[int] = []

    for i in sorted(local, key=lambda i: -waveform[i]):
        if all(abs(times[i] - times[j]) >= min_interval_s for j in picked):
            picked.append(i)

    return picked


class Judge:
    """One judge per input stream. `baseline=None` keeps NO_SIGNAL with reason "no baseline"."""

    def __init__(self, config: JudgeConfig, baseline: QuietBaseline | None) -> None:
        self.config = config
        self.baseline = baseline
        self.state = State.NO_SIGNAL
        self._windower = Windower(config)
        self._tracker = BreathTracker(config)
        self._streak = 0
        self._no_line_streak = 0  # consecutive scored windows without a line while BREATHING
        self._apnea_since: float | None = None  # the last breath before the gap that gave APNEA
        self._last_score: WindowScore | None = None
        self._last_line = False
        self._last_input: float | None = None  # time of the latest record, or of the first heartbeat before any
        self._relearn = False  # BREATHING resumed after an input gap: no gap counting until the tracker relearns
        self._last_tick_time = -math.inf

    def feed(self, record: CsiRecord) -> Tick | None:
        amplitude = np.abs(record.h).astype(np.float64)

        if self._last_input is not None and record.time - self._last_input >= self.config.input_stale_s:
            self._resume_after_gap(record.time)

        self._last_input = record.time
        self._tracker.feed(record.time, amplitude)

        if self.state is State.BREATHING and not self._relearn:
            gap = record.time - self._tracker.last_breath_time

            if transition(self.state, False, gap >= self.config.apnea_gap_s) is State.APNEA:
                self.state = State.APNEA
                self._apnea_since = self._tracker.last_breath_time
                self._streak = 0
                return self._tick(record.time, f"gap {gap:.1f}s")

        window = self._windower.feed(record.time, amplitude)

        if window is None:
            return None

        return self._judge_window(window)

    def _resume_after_gap(self, time: float) -> None:
        """Input loss never changes the state (spec), so a breath gap is only counted over
        continuous input: the tracker restarts at the first record after the loss and, in
        BREATHING, the gap is measured from that record."""
        self._tracker = BreathTracker(self.config)
        self._windower = Windower(self.config)
        self._streak = 0
        self._no_line_streak = 0  # windows before and after the loss are not consecutive

        if self.state is State.BREATHING:
            self._tracker.last_breath_time = time
            self._relearn = True

    def heartbeat(self, now: float) -> Tick | None:
        """A live source's pulse while no record arrives. Reports "no input" once per step
        after `input_stale_s`; the state never changes here."""
        if self._last_input is None:
            self._last_input = now

        if self.input_ok(now) or now - self._last_tick_time < self.config.step_s:
            return None

        return self._tick(now, "no input", input_ok=False)

    def input_age(self, now: float) -> float:
        """Seconds since the latest record (infinite before the first)."""
        return math.inf if self._last_input is None else now - self._last_input

    def input_ok(self, now: float) -> bool:
        return self.input_age(now) < self.config.input_stale_s

    def window_waveform(self) -> tuple[np.ndarray, np.ndarray]:
        """Display only: (times, Breathing waveform) of the buffered window."""
        return self._tracker.window_waveform()

    def run(self, records: Iterable[CsiRecord]) -> Iterator[Tick]:
        for record in records:
            tick = self.feed(record)

            if tick is not None:
                yield tick

    def _judge_window(self, window: Window) -> Tick:
        confirm = self.config.confirm_windows
        low, high = self.config.breathing_band_rpm
        score = window.score

        if score is not None and self.baseline is not None and score.motion > self.baseline.motion_threshold(self.config):
            self._streak = 0
            self._last_score = score
            self._last_line = False

            if self.state is State.BREATHING and not self._relearn:
                self._tracker.last_breath_time = window.end

            return self._tick(window.end, f"motion {score.motion:.2f} > {self.baseline.motion_threshold(self.config):.2f}")

        line = (self.baseline is not None and score is not None
                and score.score > self.baseline.threshold and low <= score.peak_rpm <= high)
        # In APNEA a window that still holds the breathing before the gap is no evidence of a return.
        fresh = self.state is not State.APNEA or window.end - self.config.window_s >= self._apnea_since
        self._streak = self._streak + 1 if line and fresh else 0
        self._last_score = score
        self._last_line = line
        before = self.state

        if before is State.BREATHING and score is not None:
            self._no_line_streak = 0 if line else self._no_line_streak + 1
        else:
            self._no_line_streak = 0

        lost = before is State.BREATHING and self._no_line_streak >= self.config.line_lost_windows
        self.state = transition(before, self._streak >= confirm, lost)

        if lost:
            # The spec's apnea: a Breathing line that was there and is gone. The peak-gap rule is the
            # fast path; this is the slow path for a segment whose waveform peaks decayed into noise.
            self._apnea_since = window.end - self.config.window_s
            self._streak = 0
            self._no_line_streak = 0
            return self._tick(window.end, f"line lost {self.config.line_lost_windows} windows")

        if self.state is State.BREATHING and before is not State.BREATHING:
            self._relearn = False
            self._tracker.start_segment(window.end)
            return self._tick(window.end, f"confirmed {confirm}/{confirm}")

        if self.state is State.BREATHING and self._relearn:
            if score is None:  # a partial window right after the resume decides nothing
                return self._tick(window.end, window.note)

            self._relearn = False

            if line:
                self._tracker.start_segment(window.end)
                return self._tick(window.end, "relearned after input gap")

            # Breathing was confirmed, the input came back and whole windows show no line: the
            # breathing is lost, so this is APNEA, counted from the resume. It takes as many windows
            # as confirming BREATHING does: one was enough on 10-19 night 03:45, when a sleeper's
            # marginal line (peak out of band for one window) became a shown APNEA after a stall.
            if self._no_line_streak < confirm:
                self._relearn = True
                return self._tick(window.end, f"no line after input gap {self._no_line_streak}/{confirm}")

            self.state = State.APNEA
            self._apnea_since = self._tracker.last_breath_time
            self._streak = 0
            return self._tick(window.end, "no line after input gap")

        if self.baseline is None:
            reason = "no baseline"
        elif score is None:
            reason = window.note
        elif before is State.BREATHING:
            reason = "line" if line else "no line"
        elif line and not fresh:
            reason = "line from before the gap"
        else:
            reason = f"line {self._streak}/{confirm}" if line else "no line"

        return self._tick(window.end, reason)

    def _tick(self, time: float, reason: str, input_ok: bool = True) -> Tick:
        score = self._last_score
        self._last_tick_time = time
        return Tick(time=time, state=self.state,
                    score=None if score is None else score.score,
                    threshold=None if self.baseline is None else self.baseline.threshold,
                    peak_rpm=None if score is None else score.peak_rpm,
                    line=self._last_line, last_breath_time=self._tracker.last_breath_time, reason=reason,
                    input_ok=input_ok)
