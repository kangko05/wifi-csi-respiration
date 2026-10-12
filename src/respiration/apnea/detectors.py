"""Shape shared by the judge and the rule: a rule maps a breathing waveform to apnea spans."""

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass
class Apnea:
    start: float     # time breathing is taken to have stopped (the last high peak)
    detected: float  # time the rule can decide, in realtime (start + minimum gap)
    end: float       # time breathing came back; the last sample when it did not


# (t, x) -> apneas. t is the waveform time axis [s], x the breathing waveform.
Detector = Callable[[np.ndarray, np.ndarray], list[Apnea]]
