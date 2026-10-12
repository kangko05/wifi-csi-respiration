"""Breathing rate and apnea detection on a WiFi CSI packet stream.

Input: Frame (one parsed CSI packet). Entry point: Monitor.

    monitor = Monitor()
    for frame in frames:              # from an adapter outside this package
        for event in monitor.push(frame):
            if event.kind == "start" and monitor.rate(event.t).state == RATE:
                ...                   # apnea
    monitor.rate()                    # breaths per minute, or unknown

apnea/  packet -> 20 Hz breathing waveform -> no high peak for 10 s (ported from
        ~/.projects/csi-apnea on 2026-10-12; desk holds 38/39, home lying 10/10)
rate/   120 s window -> sanitised 10 Hz grid -> spectrum line -> breaths per minute
        (ported from ~/.workspace/csi-occupancy-breathing on 2026-10-12)
"""

from respiration.apnea.judge import Event
from respiration.apnea.pipe import Pipe, make_pipe
from respiration.frame import Frame
from respiration.monitor import Monitor
from respiration.rate.rate import RATE, UNKNOWN, BreathingConfig, BreathingRate, breathing_rate

__all__ = [
    "BreathingConfig",
    "BreathingRate",
    "Event",
    "Frame",
    "Monitor",
    "Pipe",
    "RATE",
    "UNKNOWN",
    "breathing_rate",
    "make_pipe",
]
