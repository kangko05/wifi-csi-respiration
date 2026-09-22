"""Full-record column aggregation with explicit membership and original support."""

from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray
from scipy import signal

from .periodicity import PeriodicityResult, score_spectra


@dataclass(frozen=True)
class AggregationConfig:
    method: str = "psd_mean"
    top_k: int = 10

    def __post_init__(self):
        if self.method not in ("psd_mean", "topk_psd_mean", "band_pca"):
            raise ValueError("unknown aggregation method")
        if type(self.top_k) is not int or self.top_k < 1:
            raise ValueError("top_k must be a positive integer")


@dataclass(frozen=True)
class AggregationResult:
    source: PeriodicityResult
    periodicity: PeriodicityResult
    config: AggregationConfig
    member_bin_indices: NDArray
    member_standard_deviations: NDArray
    weights: NDArray
    selection_band_fractions: NDArray
    pca_energy_fraction: float | None


def frozen(values):
    array = np.asarray(values).copy()
    array.setflags(write=False)
    return array


def aggregate_columns(source: PeriodicityResult, config=AggregationConfig()):
    """Combine supported columns without labels, interpolation or new time cuts.

    PSD means use unit-variance columns and matching mean normalized ACFs.
    PCA weights apply to the original detrended columns, including RMS scaling.
    Channel -1 is explicitly synthetic; original columns remain in membership.
    A PSD mean has no single time waveform, so its waveform has zero columns.
    """
    if not isinstance(source, PeriodicityResult) or not isinstance(config, AggregationConfig):
        raise TypeError("expected PeriodicityResult and AggregationConfig")
    full = source.full_session
    if any(w is not full for w in source.windows):
        raise ValueError("aggregation experiment supports full-record input only")
    if config.method == "band_pca" and (source.temporal_filter is None or source.temporal_filter.kind != "bandpass"):
        raise ValueError("band_pca requires an explicitly bandpass-filtered source")
    columns = np.array([j for j,c in enumerate(full.candidates) if c.psd_hz is not None], dtype=int)
    band = (full.frequencies_hz >= source.config.min_hz) & (full.frequencies_hz <= source.config.max_hz)
    fractions = np.divide(full.psd[band].sum(axis=0), full.psd[full.frequencies_hz > 0].sum(axis=0),
                          out=np.zeros(full.psd.shape[1]), where=full.psd[full.frequencies_hz > 0].sum(axis=0) > 0)
    if config.method == "topk_psd_mean":
        columns = np.array(sorted(columns, key=lambda j: (-fractions[j], int(full.bin_indices[j])))[:config.top_k], dtype=int)
    if not len(columns):
        unavailable = replace(full, candidates=(), selected_index=None, best_index=None,
            bin_indices=frozen(np.empty(0, dtype=np.int64)), psd=frozen(np.empty((len(full.frequencies_hz), 0))),
            acf=frozen(np.empty((len(full.lags_s), 0))), processed_amplitude=frozen(np.empty((len(full.time_s), 0))),
            reasons=full.reasons if not full.candidates else ("no_nonflat_columns",))
        result = replace(source, full_session=unavailable, windows=tuple(unavailable for _ in source.windows))
        return AggregationResult(source, result, config, frozen(np.empty(0, dtype=np.int64)),
                                 frozen([]), frozen([]), frozen([]), None)
    values = full.processed_amplitude[:, columns]
    scale = np.std(values, axis=0)
    if not np.isfinite(scale).all() or np.any(scale <= 0):
        raise ValueError("nonflat columns must have positive finite variance")
    energy_fraction = None
    if config.method == "band_pca":
        normalized = values / scale
        _, singular, vt = np.linalg.svd(normalized, full_matrices=False)
        loadings = vt[0].copy()
        if loadings[np.argmax(abs(loadings))] < 0:
            loadings *= -1
        weights = loadings / scale
        waveform = values @ weights[:, None]
        energy_fraction = float(singular[0] ** 2 / np.sum(singular ** 2))
        nfft = 2 * (len(full.frequencies_hz) - 1)
        frequencies, psd = signal.periodogram(waveform, fs=full.sample_rate_hz, window="hann", nfft=nfft,
                                             detrend=False, scaling="density", axis=0)
        np.testing.assert_array_equal(frequencies, full.frequencies_hz)
        n = len(waveform)
        corr_fft = 1 << int(np.ceil(np.log2(2 * n - 1)))
        transform = np.fft.rfft(waveform, n=corr_fft, axis=0)
        corr = np.fft.irfft(abs(transform) ** 2, n=corr_fft, axis=0)[:n]
        acf = np.divide(corr, corr[:1], out=np.zeros_like(corr), where=corr[:1] > 0)
    else:
        weights = np.full(len(columns), 1. / len(columns))
        psd = np.mean(full.psd[:, columns] / scale ** 2, axis=1, keepdims=True)
        acf = np.mean(full.acf[:, columns], axis=1, keepdims=True)
        waveform = np.empty((len(full.time_s), 0))
    synthetic_bin = frozen(np.array([-1], dtype=np.int64))
    candidates, selected, best, reasons = score_spectra(
        full.frequencies_hz, psd, full.lags_s, acf, full.resolution_hz,
        float(np.ptp(full.time_s)), full.sample_rate_hz, synthetic_bin, [False], source.config,
    )
    combined = replace(full, processed_amplitude=frozen(waveform), bin_indices=synthetic_bin,
        psd=frozen(psd), acf=frozen(acf), candidates=candidates, selected_index=selected, best_index=best, reasons=reasons)
    result = replace(source, full_session=combined, windows=tuple(combined for _ in source.windows))
    return AggregationResult(source, result, config, frozen(full.bin_indices[columns]), frozen(scale),
                             frozen(weights), frozen(fractions[columns]), energy_fraction)
