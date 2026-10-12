"""Monitor: one frame window shared by the apnea pipe and the rate judge."""

import numpy as np
import pytest

from respiration import RATE, UNKNOWN, Frame, Monitor
from respiration.rate.grid import HOLD_SHORT
from synthetic_frames import BIN_COUNT, GAIN_KEY, PACKET_RATE_HZ, START_T_S, frames


def hold_frames(seconds: float, holds: list[tuple[float, float]], seed: int = 3) -> list[Frame]:
    """Breathing frames (15 /min) with the breathing switched off inside each (start, end) hold."""
    rng = np.random.default_rng(seed)
    half = BIN_COUNT // 2
    gains = rng.uniform(-1, 1, BIN_COUNT)
    out = []

    for index in range(int(seconds * PACKET_RATE_HZ)):
        t = index / PACKET_RATE_HZ
        breathing = not any(a <= t < b for a, b in holds)
        modulation = 2.0 * np.sin(2 * np.pi * 0.25 * t) if breathing else 0.0

        amps = 40 + modulation * gains + 0.3 * rng.standard_normal(BIN_COUNT)
        amps[half] = 0.0  # DC bin
        csi = amps.astype(np.complex64)

        out.append(Frame(t_s=START_T_S + t, device_time_us=int(t * 1e6), csi=csi, gain_key=GAIN_KEY))

    return out


def test_window_keeps_only_the_last_130_s():
    monitor = Monitor()

    for frame in frames(200, False, seed=1):
        monitor.push(frame)

    assert monitor.window_span_s == pytest.approx(130.0, abs=0.1)


def test_rate_reads_the_window():
    monitor = Monitor()

    assert monitor.rate().reason == HOLD_SHORT

    for frame in frames(125, True, seed=4):
        monitor.push(frame)

    result = monitor.rate()

    assert result.state == RATE
    assert result.rate_per_min == pytest.approx(15, abs=2)


def test_apnea_event_with_breathing_just_before_is_confirmed_by_the_rate():
    monitor = Monitor()
    starts = []
    ends = []

    for frame in hold_frames(300, holds=[(200.0, 225.0)]):
        for event in monitor.push(frame):
            if event.kind == "start":
                starts.append((event.t, monitor.rate(event.t), monitor.state))
            else:
                ends.append(event.t)

    assert len(starts) == 1
    event_t, rate_at_event, state_at_event = starts[0]

    assert START_T_S + 200 <= event_t <= START_T_S + 225
    assert state_at_event == "apnea"
    # 25 s without breathing inside a 120 s window still leaves the breathing line
    assert rate_at_event.state == RATE

    # Breathing came back at 225 s: one end event, and the monitor is back to normal
    assert len(ends) == 1
    assert monitor.state == "normal"


def test_rate_is_unknown_when_the_window_never_had_breathing():
    monitor = Monitor()

    for frame in frames(125, False, seed=6):
        monitor.push(frame)

    assert monitor.rate().state == UNKNOWN
