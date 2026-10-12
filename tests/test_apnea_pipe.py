"""The realtime apnea pipe on synthetic subcarrier amplitudes."""

import numpy as np

from respiration.apnea.judge import RuleJudge
from respiration.apnea.peak_gap import detect as peak_gap_detect
from respiration.apnea.pipe import Pipe, make_pipe
from respiration.apnea.preprocess import SlidingWaveform
from synthetic_frames import amplitude_frames

F_BREATH = 0.25


def subcarriers_with(gain_of_t, duration=300.0, seed=3, noise=0.05, t0=1.79e9):
    """Subcarrier amplitudes whose breathing strength is gain_of_t(t); large wall-clock times as in recordings."""
    rng = np.random.default_rng(seed)
    t = np.sort(rng.uniform(0, duration, int(duration * 100)))
    gains = rng.uniform(-1, 1, 30)
    breath = gain_of_t(t) * np.sin(2 * np.pi * F_BREATH * t)
    amps = 20 + np.outer(breath, gains) + noise * rng.standard_normal((len(t), 30))

    return t0 + t, amps


def stream(preprocess, t, amps):
    out = []

    for frame in amplitude_frames(t, amps):
        out += preprocess.push(frame)

    return np.array([o[0] for o in out]), np.array([o[1] for o in out])


def test_streaming_preprocess_uses_only_past():
    # Changing later data does not change the samples already produced
    t, amps = subcarriers_with(lambda s: np.ones_like(s), duration=200.0)
    changed = amps.copy()
    late = t - t[0] > 150
    changed[late] += 50 * np.random.default_rng(9).standard_normal(changed[late].shape)

    ta, xa = stream(SlidingWaveform(), t, amps)
    tb, xb = stream(SlidingWaveform(), t, changed)
    keep = ta < t[0] + 149

    assert np.allclose(xa[keep], xb[keep])


def test_streaming_judge_matches_batch_rule():
    # Re-running the rule every second on the past gives the same detections as one batch run
    fs = 20.0
    t = np.arange(0, 400, 1 / fs)
    stopped = ((t > 150) & (t < 175)) | ((t > 300) & (t < 330))
    x = np.sin(2 * np.pi * F_BREATH * t) * np.where(stopped, 0.0, 1.0)
    x = x + 0.02 * np.random.default_rng(0).standard_normal(len(t))

    batch = peak_gap_detect(t, x)

    judge = RuleJudge(peak_gap_detect)
    events = []
    for i in range(0, len(t), 7):
        events += judge.push(list(zip(t[i:i + 7], x[i:i + 7])))

    starts = [e.apnea.detected for e in events if e.kind == "start"]

    assert len(batch) == 2
    assert np.allclose(starts, [a.detected for a in batch], atol=0.5)
    assert all(0 <= e.t - e.apnea.detected <= 1.0 + 1e-9 for e in events if e.kind == "start")
    assert sum(e.kind == "end" for e in events) == 2


def test_pipe_reports_stop_once_from_frames():
    t, amps = subcarriers_with(lambda s: np.where((s > 200) & (s < 225), 0.0, 1.0), duration=320.0)
    pipe = make_pipe()

    events = list(pipe.run(amplitude_frames(t, amps)))
    starts = [e for e in events if e.kind == "start"]

    assert len(starts) == 1
    assert t[0] + 200 <= starts[0].apnea.detected <= t[0] + 225


def test_pipe_state_and_gap_reset():
    t, amps = subcarriers_with(lambda s: np.ones_like(s), duration=100.0)
    logs = []
    pipe = Pipe(SlidingWaveform, lambda: RuleJudge(peak_gap_detect), gap_reset_s=5.0, warmup_s=60.0, log=logs.append)

    assert pipe.state == "warming_up"

    for frame in amplitude_frames(t, amps):
        pipe.push(frame)

    assert pipe.state == "normal"

    # A 10 s gap restarts from scratch
    pipe.push(amplitude_frames(t[-1:] + 10.0, amps[-1:])[0])

    assert len(logs) == 1
    assert pipe.state == "warming_up"
