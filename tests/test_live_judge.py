"""Ticket 04: input loss on a live source, and what the display shows and hides."""

import json
from dataclasses import replace

import numpy as np
import pytest

from breathing import Judge, JudgeConfig, QuietBaseline, State, synthetic_records
from breathing.display import LiveView, render
from breathing.judge import Tick

CONFIG = JudgeConfig()


@pytest.fixture(scope="module")
def baseline() -> QuietBaseline:
    return QuietBaseline.from_records(synthetic_records(240.0, None, seed=1), CONFIG)


def test_input_loss_reports_no_input_once_per_step_without_changing_state(baseline):
    judge = Judge(CONFIG, baseline)
    ticks = list(judge.run(synthetic_records(180.0, 15.0, seed=2)))

    assert judge.state is State.BREATHING
    assert all(t.input_ok for t in ticks)

    last_record = 180.0
    assert judge.heartbeat(last_record + CONFIG.input_stale_s - 0.5) is None

    stale = judge.heartbeat(last_record + CONFIG.input_stale_s + 0.5)
    assert stale is not None
    assert (stale.state, stale.reason, stale.input_ok) == (State.BREATHING, "no input", False)
    assert json.loads(stale.to_json())["input_ok"] is False

    assert judge.heartbeat(stale.time + 1.0) is None, "one no-input tick per step"
    assert judge.heartbeat(stale.time + CONFIG.step_s).reason == "no input"
    assert judge.state is State.BREATHING


def test_one_lineless_window_after_an_input_gap_is_not_yet_apnea(baseline):
    """10-19 night 03:45: a sleeper's marginal line dropped out of band for the one window after a
    board stall, and the judge showed APNEA. A loss after a resume needs as many windows as a
    confirmation does."""
    judge = Judge(CONFIG, baseline)
    list(judge.run(synthetic_records(180.0, 15.0, seed=2)))
    assert judge.state is State.BREATHING

    resume = 180.0 + 2 * CONFIG.input_stale_s
    silent = [replace(r, time=r.time + resume) for r in synthetic_records(CONFIG.window_s, None, seed=3)]
    ticks = [t for t in (judge.feed(r) for r in silent) if t is not None]
    scored = [t for t in ticks if t.score is not None]

    assert (scored[0].state, scored[0].reason) == (State.BREATHING, f"no line after input gap 1/{CONFIG.confirm_windows}")
    assert (scored[1].state, scored[1].reason) == (State.APNEA, "no line after input gap")
    assert scored[1].last_breath_time == resume


def test_no_input_before_any_record_counts_from_the_first_heartbeat():
    judge = Judge(CONFIG, None)

    assert judge.heartbeat(1000.0) is None
    assert judge.heartbeat(1000.0 + CONFIG.input_stale_s - 1.0) is None

    tick = judge.heartbeat(1000.0 + CONFIG.input_stale_s + 1.0)
    assert tick is not None and tick.state is State.NO_SIGNAL and not tick.input_ok


def test_waveform_covers_the_buffered_window_in_any_state(baseline):
    judge = Judge(CONFIG, baseline)

    for record in synthetic_records(30.0, 15.0, seed=3):
        judge.feed(record)

    times, waveform = judge.window_waveform()
    assert judge.state is State.NO_SIGNAL
    assert len(times) == len(waveform) > 0
    assert 29.0 <= times[-1] - times[0] <= 30.0


def view(**overrides) -> LiveView:
    tick = Tick(time=100.0, state=State.BREATHING, score=9.0, threshold=4.0, peak_rpm=15.0, line=True,
                last_breath_time=98.0, reason="line", input_ok=True)
    fields = dict(title="t", clock="12:00:00", state=State.BREATHING, since_change_s=65.0, tick=tick, threshold=4.0,
                  baseline_note="baseline 1_2.json: 18 windows, median 2.10", input_ok=True, input_age_s=0.2,
                  packets_per_s=100.4, waveform_times=np.arange(0.0, 60.0, 0.05), waveform=np.sin(np.arange(0.0, 60.0, 0.05)),
                  window_s=60.0, input_buffer="", last_marker="", debug=False)
    fields.update(overrides)
    return LiveView(**fields)


def test_display_shows_state_score_and_input_but_no_breathing_rate():
    text = render(view(), {})

    assert "state BREATHING for 00:01:05" in text
    assert "score 9.00 / threshold 4.00" in text
    assert "input ok    100 pkt/s" in text
    assert "/min" not in text and "15.0" not in text, "the rate stays off the demo screen"


def test_display_debug_line_and_degraded_states():
    assert "15.0/min" in render(view(debug=True), {})
    assert "NO INPUT" in render(view(input_ok=False, input_age_s=12.0), {})
    assert "no baseline" in render(view(threshold=None, baseline_note="no baseline"), {})
    assert "NO_SIGNAL" in render(view(state=State.NO_SIGNAL, tick=None), {})
