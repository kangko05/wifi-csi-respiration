"""The apnea rule: no high breathing peak for 10 s or more.

After Jung, Yoo, Kim, Shin, "Detecting Sleep-Related Breathing Disorders Using FMCW
Radar", J. Electromagn. Eng. Sci. 23(5):437-445, 2023 (doi:10.26866/jees.2023.5.r.189),
whose sensor was a 61 GHz radar. Applied here to the CSI breathing waveform.
  M'(t) = |R(t)|                        R: the 0.1-0.6 Hz breathing waveform
  P(t)  = local maxima of M'(t), 0 elsewhere
  A_B   = 85th percentile of the non-zero P(t)
  eta(t) = 1 where P(t) < k * A_B, k = 0.4
  eta = 1 lasting 10 s or more is a disordered span

eta is defined on every sample and is 1 wherever there is no peak, so eta = 0 only at
"high peaks" (P >= k * A_B) and a span of eta = 1 is the gap between two neighbouring
high peaks. That literal reading is what is implemented.

Since |x| has a maximum on both the inhale crest and the exhale trough, one breath gives
up to two high peaks; the gap rule does not care, but these peaks are not a breath count.

The paper does not say over which span A_B is taken. Taking it over the whole recording
would use the future, so it is taken over the past baseline_s of peaks. The 10 s is the
clinical minimum apnea duration (AASM), which the paper also uses.

Measured with the realtime pipe (30 s preprocess window): desk, sitting 1 m from the
link, 39 breath holds of 20 s: 38 detected, 1 false alarm in about 1.5 h; home, lying in
the bedroom with the main board outside the room (2026-10-07 evening): 10 of 10 holds,
median delay 12-15 s after the stop. While asleep it fires 5-7 times an hour without
motion; whether those are real pauses is not known yet (no reference signal).
"""

import numpy as np
from scipy.signal import find_peaks

from respiration.apnea.detectors import Apnea

PEAK_SHARE = 0.4          # k: a peak counts when at least this share of the baseline (paper)
BASELINE_PERCENTILE = 85.0  # A_B percentile of past peak heights (paper)
MIN_GAP_S = 10.0          # minimum high-peak gap for an apnea (paper; clinical definition)
BASELINE_S = 120.0        # past span the baseline is taken over (ours; the paper does not say)
WARMUP_S = 60.0           # no detections before this much waveform (ours)


def high_peaks(t: np.ndarray, x: np.ndarray, k: float = PEAK_SHARE, percentile: float = BASELINE_PERCENTILE,
               baseline_s: float | None = BASELINE_S) -> tuple[np.ndarray, np.ndarray]:
    """Times of the high peaks of |x|, and of all peaks.

    A peak is high when its height is at least k x the baseline A_B at its own time:
    the percentile of the peak heights over the past baseline_s up to and including it.
    baseline_s None uses every peak from the start up to that time (still past only).
    """
    magnitude = np.abs(x)
    indices, _ = find_peaks(magnitude)   # samples above both neighbours (a flat top counts once)
    peak_t = t[indices]
    peak_h = magnitude[indices]

    keep = np.zeros(len(indices), dtype=bool)
    lo = 0

    for i in range(len(indices)):
        if baseline_s is not None:
            while peak_t[lo] < peak_t[i] - baseline_s:
                lo += 1

        baseline = np.percentile(peak_h[lo:i + 1], percentile)
        keep[i] = peak_h[i] >= k * baseline

    return peak_t[keep], peak_t


def detect(t: np.ndarray, x: np.ndarray, k: float = PEAK_SHARE, percentile: float = BASELINE_PERCENTILE,
           min_dur_s: float = MIN_GAP_S, baseline_s: float | None = BASELINE_S,
           warmup_s: float = WARMUP_S) -> list[Apnea]:
    """Apneas of a breathing waveform: every gap between high peaks of min_dur_s or more.

    start = the high peak before the gap; detected = start + min_dur_s (when the gap is
    certain); end = the next high peak, or the last sample when none came. Nothing is
    reported within the first warmup_s.
    """
    if len(t) == 0:
        return []

    kept, _ = high_peaks(t, x, k, percentile, baseline_s)

    if len(kept) == 0:
        return []

    edges = np.concatenate([kept, [t[-1]]])
    found = []

    for a, b in zip(edges, edges[1:]):
        detected = a + min_dur_s

        if b - a >= min_dur_s and detected >= t[0] + warmup_s:
            found.append(Apnea(float(a), float(detected), float(b)))

    return found
