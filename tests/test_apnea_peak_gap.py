"""The peak-gap rule on synthetic breathing waveforms."""

import numpy as np

from respiration.apnea.peak_gap import detect, high_peaks

FS = 20.0
F_BREATH = 0.25  # peaks at 1 + 4k seconds


def breath(duration=320.0, flats=(), shallow=(), seed=0):
    """0.25 Hz breathing. flats are stops (noise only), shallow spans have amplitude 0.1."""
    t = np.arange(0, duration, 1 / FS)
    amp = np.ones_like(t)

    for a, b in flats:
        amp[(t >= a) & (t < b)] = 0.0

    for a, b in shallow:
        amp[(t >= a) & (t < b)] = 0.1

    rng = np.random.default_rng(seed)
    x = amp * np.sin(2 * np.pi * F_BREATH * (t - 1) + np.pi / 2) + 0.02 * rng.standard_normal(len(t))

    return t, x


def overlaps(events, a, b):
    return [e for e in events if e.start < b and e.end > a]


def test_threshold_is_04_of_baseline():
    # 0.5 x baseline breathing still gives high peaks, 0.3 x does not
    t, x = breath()

    for a, b, gain in [(141.5, 171.5, 0.5), (221.5, 251.5, 0.3)]:
        inside = (t >= a) & (t < b)
        x[inside] *= gain

    events = detect(t, x)

    assert overlaps(events, 145, 168) == []
    assert len(overlaps(events, 225, 248)) == 1


def test_negative_lobes_count_as_breath():
    # The paper uses peaks of |R(t)|: a waveform swinging only negative is still breathing
    t, x = breath()

    assert detect(t, -np.abs(x)) == []

    kept, _ = high_peaks(t, x)
    sign = np.sign(np.interp(kept, t, x))

    assert (sign > 0).sum() > 30 and (sign < 0).sum() > 30


def test_no_detection_in_warmup():
    t, x = breath(flats=[(21.5, 41.5)])

    assert [e for e in detect(t, x) if e.detected < 60] == []


def test_detection_uses_only_past():
    # Cutting the data after the detection time gives the same detection at the same time
    t, x = breath(flats=[(201.5, 221.5)])
    full = overlaps(detect(t, x), 201.5, 221.5)

    assert len(full) == 1

    cut = t <= full[0].detected + 0.05
    part = overlaps(detect(t[cut], x[cut]), 201.5, 221.5)

    assert len(part) == 1
    assert part[0].start == full[0].start
    assert part[0].detected == full[0].detected


def test_later_large_amplitude_does_not_change_earlier_result():
    t, x = breath(flats=[(141.5, 161.5)])
    y = x.copy()
    y[t >= 200] *= 10

    def before(events):
        return [(e.start, e.detected) for e in events if e.detected < 199]

    assert before(detect(t, x)) == before(detect(t, y))


def test_baseline_follows_lasting_amplitude_drop():
    # A lasting drop to 0.3 x is flagged once; the 120 s baseline then follows it down
    t, x = breath(duration=500)
    x[t >= 150.5] *= 0.3

    events = detect(t, x)

    assert len(events) == 1
    assert 150 <= events[0].detected <= 162
    assert events[0].end < 300
