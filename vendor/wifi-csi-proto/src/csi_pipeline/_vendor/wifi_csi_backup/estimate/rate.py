"""Breathing-rate estimation.

Two independent estimators run on every capture and their agreement is the
confidence signal.  Peak counting is not a fallback: on a 60 s ground-truth
capture (19 breaths counted by hand) the Welch spectral peak said 8.4 bpm while
peak counting said 20.1 bpm, because a low-frequency artefact at the passband
edge dominated the spectrum.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage, signal

from ..features import doppler
from ..preprocess import filters
from ..preprocess.filters import BAND_HIGH, BAND_LOW
from . import quality


@dataclass
class RateEstimate:
    bpm: float
    method: str
    pm: float
    confident: bool
    spectral_bpm: float
    peak_count_bpm: float
    sharpness: float  # whitened Doppler peak height -- the confidence gate
    agreement: float  # |difference| in bpm between the two estimators
    n_peaks: int
    detail: dict


# Median-filter width used to estimate the spectral background, in bins.
WHITEN_WIDTH = 61


def whiten(power: np.ndarray, width: int = WHITEN_WIDTH) -> np.ndarray:
    """Divide the spectrum by a median-filtered copy of itself.

    Picking the raw maximum picks whatever has the most absolute power, and
    below ~0.15 Hz that is drift: a broad 1/f shoulder, not a line.  A real
    breathing rate shows up as a narrow peak a few bins wide, so normalising by
    the local background scores shape rather than magnitude.

    Measured need: a 41 bpm capture had its true line at 40.5 bpm (0.82 of the
    maximum) beaten by a 3-9 bpm drift plateau, and read 7.2 bpm.  Whitening
    recovers 40.5 and leaves the other four ground-truthed captures unchanged.
    """
    background = ndimage.median_filter(power, size=width, mode="nearest")
    floor = max(background.max() * 1e-6, np.finfo(float).tiny)
    return power / np.maximum(background, floor)


def spectral_bpm(H: np.ndarray, fs: float) -> tuple[float, np.ndarray, np.ndarray]:
    """Coherent Doppler peak over the complex CSI (Ratnam eq 30)."""
    freqs, power = doppler.spectrum(H, fs)
    return float(freqs[whiten(power).argmax()] * 60), freqs, power


def peak_count_bpm(
    x: np.ndarray, fs: float, around_bpm: float | None = None
) -> tuple[float, np.ndarray]:
    """Count peaks in the waveform and divide by elapsed time.

    With ``around_bpm`` the waveform is first band-passed to 0.7-1.5x that
    rate.  Counting over the full 3-60 bpm search band lets high-frequency
    noise register as breaths (a true 16 bpm capture counted 20.5), but a
    window this wide still excludes the 2x and 0.5x harmonic errors that are
    the failure mode the cross-check exists to catch -- so it stays a real
    check, not a restatement of the spectral answer.
    """
    if around_bpm and around_bpm > 0:
        f0 = around_bpm / 60
        try:
            x = filters.bandpass(
                x, fs, max(0.7 * f0, BAND_LOW), min(1.5 * f0, BAND_HIGH)
            )
        except ValueError:
            pass

    min_distance = max(1, int(round(fs / BAND_HIGH)))
    peaks, _ = signal.find_peaks(x, distance=min_distance, prominence=0.3 * np.std(x))
    if peaks.size < 2:
        return 0.0, peaks
    # Interval between first and last peak, not the whole record: partial
    # cycles at the ends would otherwise bias the rate down.
    span = (peaks[-1] - peaks[0]) / fs
    return float((peaks.size - 1) / span * 60) if span > 0 else 0.0, peaks


def estimate(H: np.ndarray, waveform: np.ndarray, fs: float) -> RateEstimate:
    """Run both estimators and decide.

    The coherent Doppler estimate is primary.  Peak counting is the cross-check
    that sets confidence, not a veto: near the low band edge the band-pass has
    already attenuated the fundamental, so peak counting turns noisy there
    exactly where it would be overriding a correct spectral answer.

    The older "prefer peak counting over the spectral peak" finding came from
    Welch applied to an amplitude waveform, which is a different estimator; it
    does not transfer to this one.
    """
    s_bpm, freqs, power = spectral_bpm(H, fs)
    p_bpm, peaks = peak_count_bpm(waveform, fs, around_bpm=s_bpm)
    pm = quality.peak_to_median(waveform, fs)
    sharpness = float(whiten(power).max())

    # Pegged at an extreme grid bin means the true rate is probably outside the
    # search band, so the peak is leakage rather than a real line.
    pegged = s_bpm <= freqs[0] * 60 + 1e-6 or s_bpm >= freqs[-1] * 60 - 1e-6

    if s_bpm > 0 and not pegged:
        bpm, method = s_bpm, "doppler_spectrum"
    elif p_bpm > 0:
        bpm, method = p_bpm, "peak_count (spectral peak pegged at band edge)"
    else:
        bpm, method = s_bpm, "doppler_spectrum"

    agreement = abs(s_bpm - p_bpm) if (s_bpm > 0 and p_bpm > 0) else float("inf")

    return RateEstimate(
        bpm=bpm,
        method=method,
        pm=pm,
        sharpness=sharpness,
        # Gate on sharpness, not pm.  The agreement check stays because it
        # catches 2x/0.5x harmonic errors, but it is not a detection test on
        # its own: the measured null passed it at 0.65, since band-passing
        # around the candidate makes peak counting follow whatever the
        # spectrum picked.
        confident=sharpness > quality.SHARPNESS_THRESHOLD and agreement < 3.0,
        spectral_bpm=s_bpm,
        peak_count_bpm=p_bpm,
        agreement=agreement,
        n_peaks=int(peaks.size),
        detail={
            "band_snr": quality.band_snr(freqs, power, s_bpm / 60),
            "freqs": freqs,
            "power": power,
        },
    )
