"""Monitor: the one frame window, the apnea pipe reading its newest frame, the rate judge reading all of it.

    monitor.push(frame)   store the frame, let the apnea pipe consume it -> apnea events
    monitor.rate()        breathing rate of the last RATE_WINDOW_S (computed on demand)
    monitor.state         "warming_up" | "normal" | "apnea"

Only the window holds frames. The apnea pipe keeps derived state (filter, principal
component, waveform) and never keeps a frame. An apnea start event is worth a rate()
call: a start while the rate is "unknown" came from a window without clear breathing
(motion, no one there, a weak link) and is not an apnea.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from respiration.apnea.judge import Event
from respiration.apnea.pipe import Pipe, make_pipe
from respiration.frame import Frame
from respiration.rate.rate import BreathingConfig, BreathingRate, breathing_rate

RATE_WINDOW_S = 120.0  # window the rate is read from
WINDOW_MARGIN_S = 10.0  # extra seconds kept so a full RATE_WINDOW_S can always be cut


class Monitor:
    def __init__(
        self,
        rate_window_s: float = RATE_WINDOW_S,
        margin_s: float = WINDOW_MARGIN_S,
        rate_config: BreathingConfig = BreathingConfig(),
        apnea_pipe: Pipe | None = None,
        log: Callable[[str], None] = lambda msg: None,
    ):

        self.rate_window_s = rate_window_s
        self.keep_s = rate_window_s + margin_s
        self.rate_config = rate_config
        self.window: deque[Frame] = deque()

        if apnea_pipe is None:
            apnea_pipe = make_pipe(log=log)
        self.apnea = apnea_pipe

    @property
    def state(self) -> str:
        return self.apnea.state

    @property
    def window_span_s(self) -> float:
        if len(self.window) < 2:
            return 0.0

        return self.window[-1].t_s - self.window[0].t_s

    def push(self, frame: Frame) -> list[Event]:
        """Store one frame and run the apnea pipe on it. Returns the apnea events it produced."""
        self.window.append(frame)

        while self.window and frame.t_s - self.window[0].t_s > self.keep_s:
            self.window.popleft()

        return self.apnea.push(self.window[-1])

    def rate(self, end_t_s: float | None = None) -> BreathingRate:
        """Breathing rate of the rate_window_s ending at end_t_s (default: the newest frame)."""
        if not self.window:
            return breathing_rate([], self.rate_window_s, self.rate_config)

        if end_t_s is None:
            end_t_s = self.window[-1].t_s

        frames = [
            f for f in self.window if end_t_s - self.rate_window_s < f.t_s <= end_t_s
        ]

        return breathing_rate(frames, self.rate_window_s, self.rate_config)
