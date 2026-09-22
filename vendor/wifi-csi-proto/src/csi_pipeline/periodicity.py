"""Amplitude baseline: bounded interpolation, anti-aliasing, PSD and ACF.

No labels enter this module. A periodic candidate is not a breathing detection.
All thresholds are provisional, fixed before evaluating the manual counts.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import signal

from .amplitude import AmplitudeSeries, AmplitudeSupport, amplitude_from_gain, assess_amplitude_support
from .quality import QualityResult, WindowQuality
from .temporal_filter import TemporalFilterConfig, apply_temporal_filter


@dataclass(frozen=True)
class PeriodicityConfig:
    min_hz: float = 0.05
    max_hz: float = 0.8
    target_rate_hz: float = 10.0
    max_interpolation_gap_s: float = 0.1
    filter_half_width_s: float = 1.0
    min_duration_s: float = 20.0
    min_cycles: float = 3.0
    min_acf: float = 0.3
    min_concentration: float = 0.35
    agreement_resolution_bins: float = 1.0
    flat_relative_floor: float = 1e-8
    fft_oversampling: int = 4

    def __post_init__(self):
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.min_hz < self.max_hz < 0.4 * self.target_rate_hz:
            raise ValueError("require min_hz < max_hz < 0.4 * target_rate_hz")
        if self.min_acf > 1 or self.min_concentration > 1:
            raise ValueError("evidence fractions must not exceed 1")
        if type(self.fft_oversampling) is not int:
            raise ValueError("fft_oversampling must be an integer")


@dataclass(frozen=True)
class BinCandidate:
    bin_index: int
    psd_hz: float | None
    acf_hz: float | None
    acf_peak: float | None
    concentration: float | None
    score: float
    cycles: float | None
    half_frequency_power_ratio: float | None
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PeriodicityWindow:
    window: WindowQuality
    time_s: NDArray
    processed_amplitude: NDArray
    bin_indices: NDArray
    frequencies_hz: NDArray
    psd: NDArray
    lags_s: NDArray
    acf: NDArray
    candidates: tuple[BinCandidate, ...]
    selected_index: int | None
    best_index: int | None
    reasons: tuple[str, ...]
    input_grid_time_s: NDArray
    interpolated_mask: NDArray
    sample_rate_hz: float
    resolution_hz: float | None
    filter_edge_trim_s: float
    amplitude_support: AmplitudeSupport | None = None

    @property
    def selected(self) -> BinCandidate | None:
        return self.candidates[self.selected_index] if self.selected_index is not None else None

    @property
    def best(self) -> BinCandidate | None:
        return self.candidates[self.best_index] if self.best_index is not None else None


@dataclass(frozen=True)
class PeriodicityResult:
    source: QualityResult
    config: PeriodicityConfig
    full_session: PeriodicityWindow
    windows: tuple[PeriodicityWindow, ...]
    amplitude: AmplitudeSeries
    temporal_filter: TemporalFilterConfig | None = None


def _freeze(array):
    array.setflags(write=False)
    return array


def _empty(window, reason, support=None):
    return PeriodicityWindow(
        window, _freeze(np.empty(0)), _freeze(np.empty((0, 0))),
        _freeze(np.empty(0, dtype=np.int64)), _freeze(np.empty(0)),
        _freeze(np.empty((0, 0))), _freeze(np.empty(0)), _freeze(np.empty((0, 0))),
        (), None, None, (reason,), _freeze(np.empty(0)),
        _freeze(np.empty((0, 0), dtype=bool)), 0.0, None, 0.0, support,
    )


def _analyze_window(quality, window, config, amplitude_series, temporal_filter=None):
    support = assess_amplitude_support(
        amplitude_series, quality, window, config.max_interpolation_gap_s,
    )
    if not window.data_usable:
        return _empty(window, "data_unusable", support)
    gain = quality.source
    session = gain.source
    times = session.time_s[window.row_slice]
    mask = amplitude_series.valid_mask[window.row_slice]
    # The stricter interpolation limit can exclude a bin accepted by stage 3.
    columns = np.flatnonzero(support.eligible_bin_mask)
    if not columns.size:
        reason = "no_bins_with_bounded_gaps" if support.retention_eligible_mask.any() else "no_bins_with_sufficient_retention"
        return _empty(window, reason, support)
    start = max(float(times[mask[:, col]][0]) for col in columns)
    end = min(float(times[mask[:, col]][-1]) for col in columns)
    native_rate = 1 / quality.nominal_interval_s
    down = max(1, round(native_rate / config.target_rate_hz))
    output_rate = native_rate / down
    if config.max_hz >= 0.4 * output_rate:
        return _empty(window, "sample_rate_too_low", support)
    half_length = max(1, int(np.ceil(config.filter_half_width_s * native_rate)))
    edge_s = half_length / native_rate
    if end - start - 2 * edge_s < config.min_duration_s:
        return _empty(window, "insufficient_duration_after_edge_trim", support)
    grid = start + np.arange(int(np.floor((end - start) * native_rate)) + 1) / native_rate
    amplitude = amplitude_series.values[window.row_slice]
    regular = np.column_stack([
        np.interp(grid, times[mask[:, col]], amplitude[mask[:, col], col])
        for col in columns
    ])
    # Mark exact source observations separately from interpolated grid values.
    interpolated = np.ones(regular.shape, dtype=bool)
    for j, col in enumerate(columns):
        observed = times[mask[:, col]]
        indices = np.searchsorted(observed, grid)
        right = np.minimum(indices, len(observed) - 1)
        left = np.maximum(indices - 1, 0)
        distance = np.minimum(np.abs(grid - observed[right]), np.abs(grid - observed[left]))
        interpolated[:, j] = distance > 1e-9
    # Explicit symmetric FIR makes the edge support and anti-alias filter
    # reproducible. The retained output uses no samples outside this window.
    taps = signal.firwin(
        2 * half_length + 1, 0.4 * output_rate,
        fs=native_rate, window=("kaiser", 8.0),
    )
    reduced = signal.resample_poly(regular, 1, down, axis=0, window=taps, padtype="line")
    reduced_time = grid[0] + np.arange(len(reduced)) / output_rate
    keep = (reduced_time >= grid[0] + edge_s) & (reduced_time <= grid[-1] - edge_s)
    reduced, reduced_time = reduced[keep], reduced_time[keep]
    if temporal_filter is not None:
        if output_rate <= 2 * temporal_filter.high_hz:
            return _empty(window, "sample_rate_too_low", support)
        reduced, reduced_time, additional_edge_s = apply_temporal_filter(
            reduced, reduced_time, output_rate, temporal_filter,
        )
        edge_s += additional_edge_s
    if len(reduced) < 3 or reduced_time[-1] - reduced_time[0] < config.min_duration_s:
        return _empty(window, "insufficient_duration_after_edge_trim", support)
    processed = signal.detrend(reduced, axis=0, type="linear")
    processed -= processed.mean(axis=0)
    n = len(processed)
    resolution = output_rate / n  # Zero padding does not improve this resolution.
    nfft = 1 << int(np.ceil(np.log2(n * config.fft_oversampling)))
    frequencies, psd = signal.periodogram(
        processed, fs=output_rate, window="hann", nfft=nfft,
        detrend=False, scaling="density", axis=0,
    )
    corr_fft = 1 << int(np.ceil(np.log2(2 * n - 1)))
    transform = np.fft.rfft(processed, n=corr_fft, axis=0)
    correlation = np.fft.irfft(np.abs(transform) ** 2, n=corr_fft, axis=0)[:n]
    acf = np.divide(
        correlation, correlation[0:1], out=np.zeros_like(correlation),
        where=correlation[0:1] > 0,
    )  # Biased normalization; no n/(n-lag) amplification of long lags.
    lags = np.arange(n) / output_rate
    band = np.flatnonzero((frequencies >= config.min_hz) & (frequencies <= config.max_hz))
    if not band.size:
        return _empty(window, "no_spectrum_in_band", support)
    flat_mask = [np.std(processed[:, j]) <= config.flat_relative_floor * max(1.0, np.median(reduced[:, j]))
                 for j in range(len(columns))]
    candidates, selected, best, reasons = score_spectra(
        frequencies, psd, lags, acf, resolution, reduced_time[-1] - reduced_time[0],
        output_rate, session.bin_indices[columns], flat_mask, config,
    )
    return PeriodicityWindow(
        window, _freeze(reduced_time), _freeze(processed),
        _freeze(session.bin_indices[columns].copy()), _freeze(frequencies), _freeze(psd),
        _freeze(lags), _freeze(acf), tuple(candidates), selected, best, reasons,
        _freeze(grid), _freeze(interpolated), output_rate, resolution, edge_s, support,
    )


def score_spectra(frequencies, psd, lags, acf, resolution, duration_s,
                  output_rate, bin_indices, flat_mask, config):
    """Apply the existing PSD/ACF rules to real columns or an explicit aggregate."""
    n = len(lags)
    band = np.flatnonzero((frequencies >= config.min_hz) & (frequencies <= config.max_hz))
    candidates = []
    for j, bin_index in enumerate(bin_indices):
        if flat_mask[j]:
            candidates.append(BinCandidate(int(bin_index), None, None, None, None,
                                           0.0, None, None, "flat_signal", ("flat_signal",)))
            continue
        peak = int(band[np.argmax(psd[band, j])])
        frequency = float(frequencies[peak])
        neighborhood = (np.abs(frequencies - frequency) <= resolution) & (frequencies > 0)
        # Fraction of all non-DC power, not power normalized only inside the
        # search band. Out-of-band fluctuations remain visible to this metric.
        concentration = float(psd[neighborhood, j].sum() / psd[1:, j].sum())
        peaks, _ = signal.find_peaks(acf[:, j])
        valid = peaks[
            (lags[peaks] >= 1 / config.max_hz)
            & (lags[peaks] <= min(1 / config.min_hz, (n - 1) / (2 * output_rate)))
            & (acf[peaks, j] > 0)
        ]
        acf_index = int(valid[np.argmax(acf[valid, j])]) if valid.size else None
        acf_frequency = None
        if acf_index is not None:
            before, center, after = acf[acf_index - 1:acf_index + 2, j]
            curvature = before - 2 * center + after
            offset = np.clip(0.5 * (before - after) / curvature, -0.5, 0.5) if curvature < 0 else 0.0
            acf_frequency = float(output_rate / (acf_index + offset))
        acf_peak = float(acf[acf_index, j]) if acf_index is not None else None
        cycles = frequency * duration_s
        half_ratio = None
        if frequency / 2 >= config.min_hz:
            half_band = np.abs(frequencies - frequency / 2) <= resolution / 2
            if np.any(half_band):
                half_ratio = float(psd[half_band, j].max() / psd[peak, j])
        reasons = []
        if peak in (band[0], band[-1]):
            reasons.append("search_band_edge")
        if cycles < config.min_cycles:
            reasons.append("too_few_cycles")
        if concentration < config.min_concentration:
            reasons.append("diffuse_spectrum")
        if acf_peak is None or acf_peak < config.min_acf:
            reasons.append("weak_autocorrelation")
        if acf_frequency is None or abs(frequency - acf_frequency) > config.agreement_resolution_bins * resolution:
            reasons.append("psd_acf_disagreement")
        score = concentration * max(0.0, acf_peak or 0.0)
        candidates.append(BinCandidate(
            int(bin_index), frequency, acf_frequency, acf_peak,
            concentration, score, cycles, half_ratio,
            "periodic_candidate" if not reasons else "weak_evidence", tuple(reasons),
        ))
    nonflat = [i for i, c in enumerate(candidates) if c.psd_hz is not None]
    accepted = [i for i, c in enumerate(candidates) if c.status == "periodic_candidate"]
    best = max(nonflat, key=lambda i: candidates[i].score) if nonflat else None
    selected = max(accepted, key=lambda i: candidates[i].score) if accepted else None
    reasons = () if selected is not None else ("no_supported_periodic_candidate",)
    return tuple(candidates), selected, best, reasons


def estimate_periodicity(
    quality: QualityResult, config: PeriodicityConfig = PeriodicityConfig(),
    *, amplitude: AmplitudeSeries | None = None,
    temporal_filter: TemporalFilterConfig | None = None,
) -> PeriodicityResult:
    """Extract label-free per-bin candidates from existing quality windows.

    The whole-session comparison is separate from sliding windows. No full-file
    reference rate is copied to a window. There is no temporal smoothing of
    estimates or reuse of previous values when current evidence is weak.
    An explicit amplitude input preserves preprocessing masks and signed PCA
    values. Whole-session WiCyclops preprocessing is offline: a sliding result
    may depend on observations outside that window through preprocessing.
    Optional temporal filtering follows anti-aliasing and precedes detrending;
    finite support is removed from both ends without extending any window.
    """
    if not isinstance(quality, QualityResult):
        raise TypeError("estimate_periodicity expects QualityResult")
    if not isinstance(config, PeriodicityConfig):
        raise TypeError("config must be PeriodicityConfig")
    if temporal_filter is not None and not isinstance(temporal_filter, TemporalFilterConfig):
        raise TypeError("temporal_filter must be TemporalFilterConfig or None")
    if amplitude is None:
        amplitude = amplitude_from_gain(quality.source)
    if not isinstance(amplitude, AmplitudeSeries):
        raise TypeError("amplitude must be AmplitudeSeries")
    if amplitude.source is not quality.source.source:
        raise ValueError("amplitude and quality must refer to the same SessionData object")
    expected_gain = amplitude.method if amplitude.method in ("none", "firmware") else "none"
    if quality.source.config.method != expected_gain:
        raise ValueError("amplitude method conflicts with quality gain path; do not chain gain methods")
    full = _analyze_window(quality, quality.full_session, config, amplitude, temporal_filter)
    windows = tuple(
        full if window is quality.full_session else _analyze_window(quality, window, config, amplitude, temporal_filter)
        for window in quality.windows
    )
    return PeriodicityResult(quality, config, full, windows, amplitude, temporal_filter)


def summarize_periodicity(window: PeriodicityWindow) -> dict:
    """Evidence summary; candidate_bpm is intentionally optional."""
    selected, best = window.selected, window.best
    support = window.amplitude_support
    example = selected or best
    supported = (
        sum(c.psd_hz is not None and abs(c.psd_hz - selected.psd_hz) <= window.resolution_hz
            for c in window.candidates) / len(window.candidates)
        if selected is not None else None
    )
    return {
        "start_s": window.window.start_s, "end_s": window.window.end_s,
        "status": "periodic_candidate" if selected else ("weak_evidence" if best else "unavailable"),
        "candidate_bpm": selected.psd_hz * 60 if selected else None,
        "selected_bin": selected.bin_index if selected else None,
        "best_psd_bpm": best.psd_hz * 60 if best else None,
        "best_bin": best.bin_index if best else None,
        "example_acf_bpm": example.acf_hz * 60 if example and example.acf_hz else None,
        "example_acf_peak": example.acf_peak if example else None,
        "example_concentration": example.concentration if example else None,
        "example_half_frequency_power_ratio": example.half_frequency_power_ratio if example else None,
        "same_frequency_bin_fraction": supported,
        "n_processed_bins": len(window.candidates),
        "n_supported_bins": sum(c.status == "periodic_candidate" for c in window.candidates),
        "analysis_duration_s": float(np.ptp(window.time_s)) if len(window.time_s) else 0.0,
        "resolution_bpm": window.resolution_hz * 60 if window.resolution_hz else None,
        "effective_rate_hz": window.sample_rate_hz,
        "filter_edge_trim_s": window.filter_edge_trim_s,
        "interpolated_grid_fraction": float(window.interpolated_mask.mean()) if window.interpolated_mask.size else None,
        "reasons": "|".join(window.reasons if not best else (example.reasons if example else ())),
        "quality_warnings": "|".join(window.window.warnings),
        "preprocessing_retained_fraction": support.retained_fraction_of_input if support else None,
        "n_bins_after_retention": int(support.retention_eligible_mask.sum()) if support else None,
        "n_bins_after_gap_check": int(support.eligible_bin_mask.sum()) if support else None,
        "preprocessing_max_gap_s": support.max_gap_among_input_eligible_bins_s if support else None,
    }
