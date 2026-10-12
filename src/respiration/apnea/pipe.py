"""Pipe: frames -> breathing waveform -> apnea events, with a state.

state:
  warming_up  less than WARMUP_S of waveform so far (just started, or just reconnected);
              judgements are not trusted yet
  normal      breathing
  apnea       an apnea is open (start was emitted, end not yet)

When the input stops for more than GAP_RESET_S the preprocess and judge are rebuilt:
a filter state and a peak baseline from before the gap would be wrong after it.
"""

from typing import Callable, Iterable, Iterator

from respiration.apnea.judge import Event, RuleJudge
from respiration.frame import Frame
from respiration.apnea.peak_gap import detect as peak_gap_detect
from respiration.apnea.preprocess import SlidingWaveform

WARMUP_S = 60.0           # waveform needed before judgements are trusted (30 s preprocess + 30 s baseline)
GAP_RESET_S = 5.0         # a packet gap longer than this restarts everything
PREPROCESS_WINDOW_S = 30.0


class Pipe:
    def __init__(self, make_preprocess: Callable[[], SlidingWaveform], make_judge: Callable[[], RuleJudge],
                 gap_reset_s: float = GAP_RESET_S, warmup_s: float = WARMUP_S,
                 log: Callable[[str], None] = lambda msg: None):
        self.make_preprocess = make_preprocess
        self.make_judge = make_judge
        self.gap_reset_s = gap_reset_s
        self.warmup_s = warmup_s
        self.log = log
        self.last_packet_t = None

        self.reset()

    def reset(self) -> None:
        self.preprocess = self.make_preprocess()
        self.judge = self.make_judge()

    @property
    def state(self) -> str:
        if self.judge.history_length_s < self.warmup_s:
            return "warming_up"

        if self.judge.in_apnea:
            return "apnea"

        return "normal"

    def push(self, frame: Frame) -> list[Event]:
        if self.last_packet_t is not None and frame.t_s - self.last_packet_t > self.gap_reset_s:
            self.log(f"input gap of {frame.t_s - self.last_packet_t:.1f} s; restarting preprocess and judge")
            self.reset()

        self.last_packet_t = frame.t_s

        return self.judge.push(self.preprocess.push(frame))

    def run(self, source: Iterable[Frame]) -> Iterator[Event]:
        for frame in source:
            yield from self.push(frame)


def make_pipe(log: Callable[[str], None] = lambda msg: None) -> Pipe:
    """The pipe as validated: 30 s preprocess window and the peak-gap rule."""
    return Pipe(
        lambda: SlidingWaveform(window_s=PREPROCESS_WINDOW_S),
        lambda: RuleJudge(peak_gap_detect),
        log=log,
    )
