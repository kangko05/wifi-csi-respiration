"""WiCyclops Section 7 equations, with explicit ESP32 sampling adaptations.

No reference labels or breathing-presence classifier enter this module.
See references/wicyclops_estimator.md for specified and unspecified choices.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.fft import next_fast_len
from scipy.signal import find_peaks

from .amplitude import AmplitudeSeries, AmplitudeSupport, assess_amplitude_support
from .quality import QualityResult, WindowQuality


def _readonly(array):
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class RateConfig:
    min_bpm: float = 10.0
    max_bpm: float = 30.0

    def __post_init__(self):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v)
               for v in (self.min_bpm, self.max_bpm)) or not 0 < self.min_bpm < self.max_bpm:
            raise ValueError("require finite 0 < min_bpm < max_bpm")


@dataclass(frozen=True)
class RateInput:
    window: WindowQuality
    support: AmplitudeSupport
    time_s: NDArray
    amplitude: NDArray
    bin_indices: NDArray
    interpolated_mask: NDArray
    sample_rate_hz: float
    reason: str | None


def prepare_rate_input(
    quality: QualityResult, window: WindowQuality, amplitude: AmplitudeSeries,
    max_gap_s: float = 0.1,
) -> RateInput:
    """Resample only eligible observations at native cadence without extrapolation.

    Original observations remain in AmplitudeSeries; interpolation positions are
    explicit here. No filtering, detrending or rate reduction is added.
    """
    if not np.isfinite(max_gap_s) or max_gap_s <= 0:
        raise ValueError("max_gap_s must be finite and positive")
    if quality.source.config.method != "none" or amplitude.method == "firmware":
        raise ValueError("Section 7 comparison requires raw or WiCyclops amplitude without firmware gain")
    support = assess_amplitude_support(amplitude, quality, window, max_gap_s)

    def empty(reason):
        return RateInput(window, support, _readonly(np.empty(0)), _readonly(np.empty((0, 0))),
                         _readonly(np.empty(0, dtype=np.int64)),
                         _readonly(np.empty((0, 0), dtype=bool)), 0.0, reason)

    if not window.data_usable:
        return empty("data_unusable")
    columns = np.flatnonzero(support.eligible_bin_mask)
    if not columns.size:
        return empty("no_bins_with_bounded_gaps" if support.retention_eligible_mask.any()
                     else "no_bins_with_sufficient_retention")
    times = amplitude.source.time_s[window.row_slice]
    values, mask = amplitude.values[window.row_slice], amplitude.valid_mask[window.row_slice]
    start = max(times[mask[:, col]][0] for col in columns)
    end = min(times[mask[:, col]][-1] for col in columns)
    interval = quality.nominal_interval_s
    if interval is None or not np.isfinite(interval) or interval <= 0:
        return empty("unknown_sample_interval")
    grid = start + np.arange(max(0, int(np.floor((end - start) / interval)) + 1)) * interval
    grid = grid[grid <= end]  # Floating-point rounding must not introduce extrapolation.
    if len(grid) < 3:
        return empty("insufficient_samples")
    regular = np.empty((len(grid), len(columns)))
    interpolated = np.ones(regular.shape, dtype=bool)
    for j, col in enumerate(columns):
        observed = times[mask[:, col]]
        regular[:, j] = np.interp(grid, observed, values[mask[:, col], col])
        indices = np.searchsorted(observed, grid)
        right, left = np.minimum(indices, len(observed) - 1), np.maximum(indices - 1, 0)
        distance = np.minimum(np.abs(grid - observed[right]), np.abs(grid - observed[left]))
        interpolated[:, j] = distance > 1e-9
    return RateInput(window, support, _readonly(grid), _readonly(regular),
                     _readonly(amplitude.source.bin_indices[columns].copy()),
                     _readonly(interpolated), float(1 / interval), None)


@dataclass(frozen=True)
class RateEstimate:
    config: RateConfig
    sample_rate_hz: float
    variance: NDArray
    bnr: NDArray
    score: NDArray
    selected_mask: NDArray
    acf: NDArray
    combined_acf: NDArray
    peak_indices: NDArray
    literal_peak: int | None
    band_limited_peak: int | None
    reason: str | None

    def summary(self, peak_rule: str) -> dict:
        if peak_rule not in ("literal", "band_limited"):
            raise ValueError("peak_rule must be literal or band_limited")
        peak = self.literal_peak if peak_rule == "literal" else self.band_limited_peak
        reason = self.reason or ("no_local_peak" if peak is None else "")
        return dict(
            peak_rule=peak_rule, status="rate_output" if peak is not None else "unavailable",
            reason=reason, candidate_bpm=60 * self.sample_rate_hz / peak if peak is not None else None,
            peak_lag_s=peak / self.sample_rate_hz if peak is not None else None,
            peak_height=float(self.combined_acf[peak]) if peak is not None else None,
            n_selected_bins=int(self.selected_mask.sum()),
            selected_score_sum=float(self.score[self.selected_mask].sum()),
            max_bnr=float(self.bnr.max()) if self.bnr.size else None,
        )


def _candidate_scores(variance, bnr):
    score = 0.3 * variance / variance.sum() + 0.7 * bnr / bnr.sum()
    return score, score > 0.7 * score.max()


def _first_peaks(combined, sample_rate_hz, config):
    peaks = find_peaks(combined)[0]
    lag = peaks / sample_rate_hz
    in_band = peaks[(lag >= 60 / config.max_bpm) & (lag <= 60 / config.min_bpm)]
    return peaks, int(peaks[0]) if peaks.size else None, int(in_band[0]) if in_band.size else None


def estimate_rate(values, sample_rate_hz: float, config: RateConfig = RateConfig()) -> RateEstimate:
    """Equations 11-13 on a finite, uniformly sampled [time, bin] real matrix.

    Returns both literal first local maximum and a band-limited first maximum.
    Neither is a validated breathing detection. Sample-discrete peaks are used.
    """
    original = np.asarray(values)
    if original.ndim != 2 or np.iscomplexobj(original) or not np.issubdtype(original.dtype, np.number):
        raise ValueError("values must be a real numeric [time, bin] matrix")
    if min(original.shape) < 1 or original.shape[0] < 3 or not np.isfinite(original).all():
        raise ValueError("values need at least three finite observations and one bin")
    if isinstance(sample_rate_hz, bool) or not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if not isinstance(config, RateConfig):
        raise TypeError("config must be RateConfig")
    if config.max_bpm / 60 >= sample_rate_hz / 2:
        raise ValueError("breathing band must lie below Nyquist")
    original = original.astype(np.float64)
    centered = original - original.mean(axis=0)
    # Only suppress floating-point residue of constant columns, not weak signals.
    scale = np.maximum(1, np.max(np.abs(original), axis=0))
    flat = np.ptp(original, axis=0) <= 8 * np.spacing(scale)
    centered[:, flat] = 0
    n, columns = centered.shape
    variance = np.mean(centered**2, axis=0)
    transform = np.fft.rfft(centered, axis=0)  # Unpadded rectangular DFT for BNR.
    energy = np.abs(transform)**2
    energy[1:(-1 if n % 2 == 0 else None)] *= 2  # Preserve real-signal total energy.
    frequency = np.fft.rfftfreq(n, 1 / sample_rate_hz)
    band = (frequency >= config.min_bpm / 60) & (frequency <= config.max_bpm / 60)
    total_energy = energy.sum(axis=0)
    bnr = np.divide(energy[band].max(axis=0), total_energy, out=np.zeros(columns),
                    where=total_energy > 0) if band.any() else np.zeros(columns)
    score = np.zeros(columns)
    reason = None
    if variance.sum() == 0:
        reason = "constant_signal"
    elif not band.any():
        reason = "no_fft_bin_in_band"
    elif bnr.sum() == 0:
        reason = "undefined_bnr_normalization"
    selected = np.zeros(columns, dtype=bool)
    if reason is None:
        score, selected = _candidate_scores(variance, bnr)
    # Padding here prevents circular correlation; it is unrelated to BNR bins.
    nfft = next_fast_len(2 * n - 1)
    spectrum = np.fft.rfft(centered, n=nfft, axis=0)
    correlation = np.fft.irfft(np.abs(spectrum)**2, n=nfft, axis=0)[:n]
    acf = np.divide(correlation, correlation[:1], out=np.zeros_like(correlation),
                    where=correlation[:1] > 0)
    combined = np.sum(acf[:, selected] * score[selected], axis=1)
    peaks, literal, band_limited = _first_peaks(combined, sample_rate_hz, config)
    return RateEstimate(config, float(sample_rate_hz), *map(_readonly, (
        variance, bnr, score, selected, acf, combined, peaks,
    )), literal, band_limited, reason)
