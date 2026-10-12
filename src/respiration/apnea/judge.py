"""Realtime judgement: breathing waveform samples in, apnea start/end events out.

RuleJudge wraps the rule (detect(t, x) -> [Apnea]) without changing it:
  - every hop_s the rule is re-run on the last history_s of waveform
  - a detection not seen before gives a "start" event; when breathing comes back for
    an open detection, an "end" event
The rule only uses data up to each detection time, so as long as history_s is well
beyond the rule's baseline window the live events land at the same times as one
offline run over the whole recording (tests/test_apnea_pipe.py checks this).
"""

from dataclasses import dataclass

import numpy as np

from respiration.apnea.detectors import Apnea, Detector

HISTORY_S = 600.0   # waveform handed to the rule; well beyond its 120 s baseline
HOP_S = 1.0         # how often the rule is re-run
MATCH_S = 1.0       # detections this close in time are the same detection


@dataclass
class Event:
    kind: str      # "start" (apnea detected) | "end" (breathing is back)
    t: float       # time the event was emitted, on the waveform time axis
    apnea: Apnea


class RuleJudge:
    def __init__(self, detect: Detector, history_s: float = HISTORY_S, hop_s: float = HOP_S,
                 match_s: float = MATCH_S):
        self.detect = detect
        self.history_s = history_s
        self.hop_s = hop_s
        self.match_s = match_s

        self.t = []                     # past waveform times
        self.x = []                     # past waveform values
        self.next_eval = None           # next time the rule runs
        self.open: list[Apnea] = []     # detections with a start emitted and no end yet
        self.last_detected = -np.inf    # latest detection time among the starts emitted

    @property
    def history_length_s(self) -> float:
        """Length of waveform held; tells whether the pipe is still warming up."""
        if len(self.t) < 2:
            return 0.0

        return self.t[-1] - self.t[0]

    @property
    def in_apnea(self) -> bool:
        return len(self.open) > 0

    def push(self, samples: list[tuple[float, float]]) -> list[Event]:
        events = []

        for t, x in samples:
            self.t.append(t)
            self.x.append(x)

            if self.next_eval is None:
                self.next_eval = t + self.hop_s

            if t >= self.next_eval:
                self.next_eval = t + self.hop_s
                events += self._evaluate()

        return events

    def _evaluate(self) -> list[Event]:
        now = self.t[-1]
        self._drop_old_samples(now)

        t = np.array(self.t)
        x = np.array(self.x)
        found = self.detect(t, x)

        return self._new_starts(found, now) + self._ends(found, now)

    def _drop_old_samples(self, now: float) -> None:
        while self.t and self.t[0] < now - self.history_s:
            self.t.pop(0)
            self.x.pop(0)

    def _new_starts(self, found: list[Apnea], now: float) -> list[Event]:
        events = []

        for apnea in found:
            is_new = self.last_detected + self.match_s < apnea.detected <= now

            if not is_new:
                continue

            events.append(Event("start", apnea.detected, apnea))
            self.open.append(apnea)
            self.last_detected = apnea.detected

        return events

    def _ends(self, found: list[Apnea], now: float) -> list[Event]:
        """For each open detection: did the rule give it again, and does it have an end now?"""
        events = []
        still_open = []

        for opened in self.open:
            same = [a for a in found if abs(a.detected - opened.detected) <= self.match_s]

            if same and same[0].end < now:
                events.append(Event("end", now, same[0]))
            elif same or opened.detected > now - self.history_s:
                still_open.append(opened)

        self.open = still_open

        return events
