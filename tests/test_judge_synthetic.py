"""Tickets 02 and 03 on synthetic records: the line rule and the state machine without a recording."""

import itertools
import json

import pytest

from breathing import Judge, JudgeConfig, QuietBaseline, State, synthetic_records

CONFIG = JudgeConfig()
BREATHS_PER_MIN = 15.0
PERIOD_S = 60.0 / BREATHS_PER_MIN


@pytest.fixture(scope="module")
def baseline() -> QuietBaseline:
    return QuietBaseline.from_records(synthetic_records(240.0, None, seed=1), CONFIG)


def ticks(baseline, records):
    return list(Judge(CONFIG, baseline).run(records))


def states_after(ticks, time):
    return {t.state for t in ticks if t.time >= time}


def last_crest_before(time: float) -> float:
    """The sine's last peak before `time` (crests at period/4 + k * period)."""
    crests = (PERIOD_S / 4.0 + k * PERIOD_S for k in itertools.count())
    return max(c for c in itertools.takewhile(lambda c: c < time, crests))


def test_breathing_sine_is_confirmed_and_never_apnea(baseline):
    out = ticks(baseline, synthetic_records(180.0, BREATHS_PER_MIN, seed=2))

    first_window = CONFIG.window_s + CONFIG.confirm_windows * CONFIG.step_s
    assert states_after(out, first_window) == {State.BREATHING}
    assert State.APNEA not in {t.state for t in out}


def test_noise_only_stays_no_signal(baseline):
    out = ticks(baseline, synthetic_records(240.0, None, seed=3))

    assert {t.state for t in out} == {State.NO_SIGNAL}


def test_pause_gives_apnea_within_20s_then_breathing_again(baseline):
    pause = (120.0, 165.0)
    out = ticks(baseline, synthetic_records(300.0, BREATHS_PER_MIN, pauses=(pause,), seed=4))

    first_apnea = next(t for t in out if t.state is State.APNEA)
    last_breath = last_crest_before(pause[0])
    assert CONFIG.apnea_gap_s <= first_apnea.time - last_breath <= 20.0
    assert out[-1].state is State.BREATHING
    after_apnea = [t.state for t in out if t.time > first_apnea.time]
    assert State.NO_SIGNAL not in after_apnea


def test_breathing_that_never_resumes_stays_apnea(baseline):
    out = ticks(baseline, synthetic_records(400.0, BREATHS_PER_MIN, pauses=((120.0, 400.0),), seed=5))

    first_apnea = next(i for i, t in enumerate(out) if t.state is State.APNEA)
    assert {t.state for t in out[first_apnea:]} == {State.APNEA}
    assert out[-1].time >= 390.0


def test_noise_then_silence_stays_no_signal(baseline):
    records = itertools.chain(synthetic_records(120.0, None, seed=6),
                              synthetic_records(120.0, None, noise=0.0, start=120.0, seed=6))
    out = ticks(baseline, records)

    assert {t.state for t in out} == {State.NO_SIGNAL}


def test_without_baseline_every_tick_says_so():
    out = ticks(None, synthetic_records(120.0, BREATHS_PER_MIN, seed=7))

    assert out and {t.state for t in out} == {State.NO_SIGNAL}
    assert {t.reason for t in out if t.time >= CONFIG.window_s} == {"no baseline"}


def test_tick_json_carries_the_log_fields(baseline):
    out = ticks(baseline, synthetic_records(90.0, BREATHS_PER_MIN, seed=8))

    line = json.loads(out[-1].to_json())
    assert set(line) == {"time", "state", "score", "threshold", "peak_rpm", "line", "last_breath_time", "reason", "input_ok"}
    assert line["state"] == "BREATHING" and line["line"] is True
    assert abs(line["peak_rpm"] - BREATHS_PER_MIN) <= 1.0
    assert line["score"] > line["threshold"]
    assert line["last_breath_time"] is not None


def with_input_gap(records, gap):
    """Drop the records inside `gap`, as a source that lost its link for a while delivers."""
    return (r for r in records if not gap[0] <= r.time < gap[1])


def test_input_gap_during_breathing_is_not_apnea(baseline):
    gap = (120.0, 600.0)
    out = ticks(baseline, with_input_gap(synthetic_records(900.0, BREATHS_PER_MIN, seed=5), gap))

    assert State.APNEA not in {t.state for t in out}
    assert out[-1].state is State.BREATHING


def test_apnea_after_input_gap_counts_from_the_resume(baseline):
    gap = (120.0, 600.0)
    records = synthetic_records(900.0, BREATHS_PER_MIN, pauses=((600.0, 900.0),), seed=6)
    out = ticks(baseline, with_input_gap(records, gap))

    first_apnea = next(t for t in out if t.state is State.APNEA)
    assert State.APNEA not in {t.state for t in out if t.time < gap[1]}
    earliest_scored_window = gap[1] + CONFIG.min_fill * CONFIG.window_s
    assert earliest_scored_window <= first_apnea.time <= gap[1] + CONFIG.window_s + 2 * CONFIG.step_s


def test_line_lost_for_nine_windows_is_apnea_even_without_a_peak_gap(baseline):
    """A segment whose waveform keeps finding peaks in noise still ends: no line for 90 s."""
    config = JudgeConfig(apnea_gap_s=1e9)
    records = synthetic_records(400.0, BREATHS_PER_MIN, pauses=((150.0, 400.0),), seed=7)
    out = list(Judge(config, baseline).run(records))

    first_apnea = next(t for t in out if t.state is State.APNEA)
    assert "line lost" in first_apnea.reason
    assert 150.0 + config.window_s <= first_apnea.time <= 150.0 + config.window_s + (config.line_lost_windows + 1) * config.step_s
