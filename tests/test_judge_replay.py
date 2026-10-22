"""Tickets 01-03 on the 10-16 meeting recording. Ground truth (KST): empty room 10:27-10:32,
doll 15/min without pause 11:00-11:06, doll with about three pauses 11:16-11:20.

A tick belongs to a segment when the window it judged lies inside the segment.
"""

import datetime as dt

import numpy as np
import pytest

from breathing import Judge, JudgeConfig, QuietBaseline, State, read_frames_recording

pytestmark = pytest.mark.replay

CONFIG = JudgeConfig()
KST = dt.timezone(dt.timedelta(hours=9))


def kst(hms: str) -> float:
    return dt.datetime.fromisoformat(f"2026-10-16T{hms}").replace(tzinfo=KST).timestamp()


EMPTY = (kst("10:27:00"), kst("10:32:00"))
DOLL = (kst("11:00:00"), kst("11:06:00"))
PAUSES = (kst("11:16:00"), kst("11:20:00"))


def replay(session_dir, until: float):
    records = list(read_frames_recording(session_dir))
    baseline = QuietBaseline.from_records((r for r in records if EMPTY[0] <= r.time < EMPTY[1]), CONFIG)
    ticks = list(Judge(CONFIG, baseline).run(r for r in records if r.time < until))
    return records, ticks


@pytest.fixture(scope="module")
def main(recording):
    return replay(recording / "main", kst("11:21:00"))


@pytest.fixture(scope="module")
def case(recording):
    return replay(recording / "case", kst("11:07:00"))


def inside(ticks, segment):
    start, end = segment
    return [t for t in ticks if start <= t.time - CONFIG.window_s and t.time <= end]


def fraction(ticks, state) -> float:
    return sum(t.state is state for t in ticks) / len(ticks)


def test_main_records_are_100hz_56_bins_on_the_wall_clock(main):
    records, _ = main
    session_start = dt.datetime.fromisoformat("2026-10-16T10:26:02.226").replace(tzinfo=KST).timestamp()
    times = np.array([r.time for r in records])

    assert records[0].h.shape == (56,)
    assert 0.0 <= times[0] - session_start <= 2.0
    assert abs(len(times) / (times[-1] - times[0]) - 100.0) < 1.0
    assert (np.diff(times) >= 0.0).all()


def test_empty_room_never_breathes(main):
    _, ticks = main
    segment = inside(ticks, EMPTY)

    assert len(segment) >= 20
    assert {t.state for t in segment} == {State.NO_SIGNAL}


def test_doll_is_breathing_and_never_enters_apnea(main):
    """The judge runs on from the empty room, so the state the doll segment starts in is
    whatever the setup activity before 11:00 left; what must not happen is an APNEA entry
    while the doll breathes."""
    _, ticks = main
    segment = inside(ticks, DOLL)

    assert len(segment) >= 30
    assert fraction(segment, State.BREATHING) >= 0.9
    steps = list(zip((t.state for t in segment), (t.state for t in segment[1:])))
    assert (State.BREATHING, State.APNEA) not in steps


def test_pauses_give_apnea_and_breathing_returns(main):
    _, ticks = main
    segment = inside(ticks, PAUSES)
    first_apnea = next(i for i, t in enumerate(segment) if t.state is State.APNEA)

    assert any(t.state is State.BREATHING for t in segment[first_apnea:])


def test_case_link_barely_sees_the_doll(case):
    """ADR 0001: the same judge on the Case link finds the Breathing line in few doll windows
    and does not call the segment BREATHING."""
    _, ticks = case
    segment = inside(ticks, DOLL)

    assert len(segment) >= 30
    assert sum(t.line for t in segment) / len(segment) < 0.5
    assert fraction(segment, State.BREATHING) < 0.5
