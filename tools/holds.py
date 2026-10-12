"""Breath-hold schedule: beeps at the planned times, the labels they imply, and the score.

Schedule (same as the csi-apnea desk sessions): first hold after first_hold_s, every
period_s, each hold_s long, count times. A hold is announced by a long beep at its
start and a short beep at its end; the labels come from the schedule, not from any
key press, so the person does not have to move. Holds the person failed (breathed
before the end beep) are entered by number afterwards and left out of the score.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

HIT_MARGIN_S = 2.0   # an apnea start inside [hold start - margin, hold end + margin] is a hit


@dataclass(frozen=True)
class HoldSchedule:
    first_hold_s: float = 100.0
    hold_s: float = 20.0
    period_s: float = 120.0
    count: int = 7

    def holds(self) -> list[tuple[float, float]]:
        """(start, end) offsets in seconds from the session start."""
        out = []

        for index in range(self.count):
            start = self.first_hold_s + index * self.period_s
            out.append((start, start + self.hold_s))

        return out

    @property
    def total_s(self) -> float:
        return self.first_hold_s + (self.count - 1) * self.period_s + self.hold_s + self.period_s / 2


class Beeper:
    """Plays a sine beep through paplay/aplay; falls back to the terminal bell."""

    def __init__(self, folder: Path | None = None, enabled: bool = True):
        self.player = shutil.which("paplay") or shutil.which("aplay")
        self.folder = Path(folder) if folder is not None else Path(tempfile.mkdtemp(prefix="beep-"))
        self.files = {}
        self.enabled = enabled

    def beep(self, seconds: float = 0.3, frequency_hz: float = 880.0) -> None:
        if not self.enabled:
            return

        if self.player is None:
            print("\a", end="", flush=True)
            return

        path = self._wav(seconds, frequency_hz)
        subprocess.Popen([self.player, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wav(self, seconds: float, frequency_hz: float) -> Path:
        key = (seconds, frequency_hz)

        if key not in self.files:
            path = self.folder / f"beep_{int(seconds * 1000)}ms_{int(frequency_hz)}hz.wav"
            rate = 44100
            count = int(seconds * rate)

            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                samples = [int(12000 * math.sin(2 * math.pi * frequency_hz * i / rate)) for i in range(count)]
                handle.writeframes(struct.pack(f"<{count}h", *samples))

            self.files[key] = path

        return self.files[key]


def score(apnea_starts_s: list[float], holds_s: list[tuple[float, float]], failed: set[int],
          margin_s: float = HIT_MARGIN_S) -> dict:
    """Hits per valid hold, false alarms, and the detection delay of each hit.

    apnea_starts_s and holds_s are on the same clock. A start inside a valid hold
    (with margin) is that hold's hit (the first one counts for the delay); any other
    start is a false alarm. Starts inside a failed hold are ignored.
    """
    valid = {index: hold for index, hold in enumerate(holds_s) if index not in failed}
    hits = {}
    false_alarms = []

    for start in apnea_starts_s:
        matched = None

        for index, (a, b) in enumerate(holds_s):
            if a - margin_s <= start <= b + margin_s:
                matched = index
                break

        if matched is None:
            false_alarms.append(start)
        elif matched in valid and matched not in hits:
            hits[matched] = start - holds_s[matched][0]

    return {
        "valid_holds": len(valid),
        "hits": len(hits),
        "delays_s": [hits[index] for index in sorted(hits)],
        "missed": [index for index in valid if index not in hits],
        "false_alarms": false_alarms,
    }
