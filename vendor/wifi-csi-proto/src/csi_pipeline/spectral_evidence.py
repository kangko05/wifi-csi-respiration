"""Label-free alternatives to spectral evidence; reuse fixed PSD/ACF and time.

Original concentration remains on each candidate for side-by-side diagnostics.
Alternative evidence changes column ranking and its spectral gate only.
"""

from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from .periodicity import PeriodicityResult


@dataclass(frozen=True)
class EvidenceConfig:
    method: str = "original"
    min_local_snr: float = 3.0
    floor_inner_bins: int = 2
    floor_outer_bins: int = 5
    min_floor_points: int = 4
    relative_floor: float = 1e-12

    def __post_init__(self):
        if self.method not in ("original", "band_concentration", "local_snr"):
            raise ValueError("unknown evidence method")
        for key in ("min_local_snr", "relative_floor"):
            v = getattr(self, key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or v <= 0:
                raise ValueError(f"{key} must be finite and positive")
        for key in ("floor_inner_bins", "floor_outer_bins", "min_floor_points"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.floor_inner_bins < 2 or self.floor_outer_bins < self.floor_inner_bins:
            raise ValueError("noise floor must exclude the peak neighborhood")
        if self.min_floor_points > 2 * (self.floor_outer_bins - self.floor_inner_bins + 1):
            raise ValueError("minimum floor points exceeds available offsets")
        if self.relative_floor >= 1:
            raise ValueError("relative floor must be below one")


@dataclass(frozen=True)
class ColumnEvidence:
    band_concentration: NDArray
    local_snr: NDArray
    floor_points: NDArray


@dataclass(frozen=True)
class EvidenceResult:
    source: PeriodicityResult
    periodicity: PeriodicityResult
    config: EvidenceConfig
    full_evidence: ColumnEvidence
    window_evidence: tuple[ColumnEvidence, ...]


def column_evidence(window, periodicity_config, config=EvidenceConfig()):
    """Measure band fraction and peak / adjacent median PSD density ratio.

    Floor samples are nearest FFT grid points at ±2..5 actual resolution bins,
    restricted to the search band. Zero padding does not add floor observations.
    Undefined values are NaN internally and exported as null by reporting code.
    """
    count = len(window.candidates)
    concentration = np.full(count, np.nan)
    snr = np.full(count, np.nan)
    counts = np.zeros(count, dtype=np.int64)
    frequencies = window.frequencies_hz
    band = (frequencies >= periodicity_config.min_hz) & (frequencies <= periodicity_config.max_hz)
    resolution = window.resolution_hz
    for j, candidate in enumerate(window.candidates):
        if candidate.psd_hz is None or resolution is None:
            continue
        power = window.psd[:, j]
        peak = int(np.argmin(abs(frequencies - candidate.psd_hz)))
        neighborhood = (abs(frequencies - candidate.psd_hz) <= resolution) & band
        total = float(power[band].sum())
        if total <= 0:
            continue
        concentration[j] = power[neighborhood].sum() / total
        indices = set()
        for offset in range(config.floor_inner_bins, config.floor_outer_bins + 1):
            for direction in (-1, 1):
                target = candidate.psd_hz + direction * offset * resolution
                if periodicity_config.min_hz <= target <= periodicity_config.max_hz:
                    index = int(np.argmin(abs(frequencies - target)))
                    if band[index] and not neighborhood[index]:
                        indices.add(index)
        counts[j] = len(indices)
        if len(indices) >= config.min_floor_points:
            floor = max(float(np.median(power[sorted(indices)])), float(power.max()) * config.relative_floor)
            if floor > 0:
                snr[j] = power[peak] / floor
    for array in (concentration, snr, counts):
        array.setflags(write=False)
    return ColumnEvidence(concentration, snr, counts)


def redefine_evidence(source: PeriodicityResult, config=EvidenceConfig()):
    """Rerank columns and replace only the spectral acceptance condition.

Band fraction uses the existing 0.35 gate. Local SNR uses the explicit ratio
gate (default 3); these are provisional gates, not calibrated confidences.
The original candidate.concentration keeps its original all-power denominator.
    """
    if not isinstance(source, PeriodicityResult) or not isinstance(config, EvidenceConfig):
        raise TypeError("expected PeriodicityResult and EvidenceConfig")

    def analyze(window):
        metrics = column_evidence(window, source.config, config)
        if config.method == "original":
            return window, metrics
        values = metrics.band_concentration if config.method == "band_concentration" else metrics.local_snr
        threshold = source.config.min_concentration if config.method == "band_concentration" else config.min_local_snr
        candidates = []
        for j, candidate in enumerate(window.candidates):
            if candidate.psd_hz is None:
                candidates.append(candidate)
                continue
            reasons = [r for r in candidate.reasons if r not in ("diffuse_spectrum", "weak_band_concentration", "weak_local_snr", "undefined_spectral_evidence")]
            value = values[j]
            if not np.isfinite(value):
                reasons.append("undefined_spectral_evidence")
            elif value < threshold:
                reasons.append("weak_band_concentration" if config.method == "band_concentration" else "weak_local_snr")
            score = float(value) * max(0., candidate.acf_peak or 0.) if np.isfinite(value) else 0.
            candidates.append(replace(candidate, score=score, reasons=tuple(reasons),
                                      status="weak_evidence" if reasons else "periodic_candidate"))
        available = [i for i, c in enumerate(candidates) if c.psd_hz is not None and np.isfinite(values[i])]
        accepted = [i for i, c in enumerate(candidates) if c.status == "periodic_candidate"]
        best = max(available, key=lambda i: candidates[i].score) if available else None
        selected = max(accepted, key=lambda i: candidates[i].score) if accepted else None
        reasons = window.reasons if not candidates else (() if selected is not None else ("no_supported_periodic_candidate",))
        return replace(window, candidates=tuple(candidates), best_index=best, selected_index=selected, reasons=reasons), metrics

    full, full_metrics = analyze(source.full_session)
    windows, metrics = [], []
    for window in source.windows:
        w, m = (full, full_metrics) if window is source.full_session else analyze(window)
        windows.append(w)
        metrics.append(m)
    return EvidenceResult(source, replace(source, full_session=full, windows=tuple(windows)), config, full_metrics, tuple(metrics))
