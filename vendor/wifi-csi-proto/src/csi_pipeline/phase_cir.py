"""Adapt the preserved backup into separate phase and CIR decisions.

Both paths share RMS gain/LoS phase correction by default, but neither decision
uses the other path's spectrum, candidate rate, waveform, or confidence. CIR
uses a newly independent application of the backup estimator to its own taps;
its inherited confidence threshold has not been calibrated for this input.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .input import SessionData
from ._vendor.wifi_csi_backup import layout
from ._vendor.wifi_csi_backup.model import CsiBlock
from ._vendor.wifi_csi_backup.preprocess import filters, gain, phase, resample
from ._vendor.wifi_csi_backup.features import cir, selection
from ._vendor.wifi_csi_backup.estimate import quality as legacy_quality, rate


class PhaseCirUnavailable(ValueError):
    """Recorded support cannot safely enter the backup's dense-array pipeline."""


def _readonly(value):
    result = np.array(value, copy=True)
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class PhaseCirConfig:
    gain_method: str = "rms_norm"
    phase_method: str = "los_wls"
    n_taps: int = 8
    top_columns: int = 5
    max_gap_s: float = .1
    min_duration_s: float = 20.0
    layout_profile: str = "backup_ht40_117_assumed"

    def __post_init__(self):
        if self.gain_method not in gain.METHODS:
            raise ValueError("unknown backup gain method")
        if self.phase_method not in phase.METHODS:
            raise ValueError("unknown backup phase method")
        for name in ("n_taps", "top_columns"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 114:
                raise ValueError(f"{name} must be an integer from 1 to 114")
        for name in ("max_gap_s", "min_duration_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.layout_profile != "backup_ht40_117_assumed":
            raise ValueError("only the explicit backup HT40 layout assumption is supported")


@dataclass(frozen=True)
class PhaseCirSupport:
    source: SessionData
    original_row_indices: NDArray
    original_bin_indices: NDArray
    assumed_frequency_offsets_hz: NDArray
    corrected_csi: NDArray
    time_s: NDArray
    uniform_csi: NDArray
    interpolated_time_mask: NDArray
    sample_rate_hz: float
    max_retained_gap_s: float
    resolution_hz: float
    layout_profile: str


@dataclass(frozen=True)
class PhaseCirDecision:
    method: str
    diagnostic_bpm: float | None
    spectral_bpm: float | None
    peak_count_bpm: float | None
    accepted_bpm: float | None
    accepted: bool
    estimate_method: str
    sharpness: float | None
    agreement_bpm: float | None
    peak_to_median: float | None
    band_snr: float | None
    n_peaks: int
    reasons: tuple[str, ...]
    waveform: NDArray
    frequencies_hz: NDArray
    spectral_power: NDArray
    whitened_power: NDArray
    selected_feature_indices: NDArray
    feature_kind: str


@dataclass(frozen=True)
class PhaseCirResult:
    config: PhaseCirConfig
    support: PhaseCirSupport
    phase: PhaseCirDecision
    cir: PhaseCirDecision
    legacy_combined: PhaseCirDecision | None


def adapt_session(source: SessionData, config: PhaseCirConfig) -> tuple[CsiBlock, NDArray, float]:
    """Use backup frequency assumptions explicitly, never infer them from zeros.

The known null pattern is a compatibility check, not proof of physical ordering.
The dense backup requires all 114 occupied bins: packets invalid in any of these
bins are excluded, with original row IDs/times retained. Gaps over the configured
limit, including excluded edges, stop processing before interpolation.
"""
    if not isinstance(source, SessionData):
        raise TypeError("phase/CIR input must be raw SessionData")
    if source.csi.shape[1] != 117:
        raise PhaseCirUnavailable("unsupported_layout_width")
    if not source.valid_packet_mask.any():
        raise PhaseCirUnavailable("no_valid_packets")
    if not np.isfinite(source.csi[source.valid_sample_mask]).all():
        raise PhaseCirUnavailable("nonfinite_valid_csi")
    try:
        layout.validate_layout(source.csi[source.valid_packet_mask], layout.HT40)
    except AssertionError as exc:
        raise PhaseCirUnavailable("backup_layout_check_failed") from exc
    columns = layout.HT40.take
    rows = np.flatnonzero(source.valid_sample_mask[:, columns].all(axis=1))
    if len(rows) < 2:
        raise PhaseCirUnavailable("insufficient_complete_packets")
    times = source.time_s[rows]
    gaps = np.diff(np.r_[source.time_s[0], times, source.time_s[-1]])
    max_gap = float(gaps.max())
    if max_gap > config.max_gap_s + 1e-9:
        raise PhaseCirUnavailable("long_gap_after_validity_mask")
    if times[-1] - times[0] < config.min_duration_s:
        raise PhaseCirUnavailable("insufficient_duration")
    if 1 / np.median(np.diff(times)) <= 2 * filters.BAND_HIGH / .99:
        raise PhaseCirUnavailable("sample_rate_too_low")
    block = CsiBlock(t=times.copy(), H=source.csi[np.ix_(rows, columns)].astype(np.complex128),
        f=layout.HT40.f.copy(), **{name:source.metadata[name][rows].copy() for name in
        ("seq", "rssi", "noise_floor", "agc_gain", "fft_gain", "compensate_gain", "dropped")})
    return block, rows, max_gap


def _finite(value):
    return float(value) if np.isfinite(value) else None


def decide_features(method, spectrum_features, waveform_features, fs, top_columns, feature_indices, feature_kind):
    """Backup estimator on one path's own inputs; no cross-path candidate prior.

Only the optional legacy control intentionally uses different spectrum/waveform
inputs. Near-zero features are marked flat instead of inventing a band-edge BPM.
"""
    selected = selection.rank(waveform_features, fs, top_columns)
    waveform = selection.combine(waveform_features, fs, top_columns)
    waveform = filters.bandpass(filters.hampel(waveform), fs)
    if max(float(np.max(np.abs(spectrum_features))), float(np.max(np.abs(waveform)))) <= 1e-12:
        return PhaseCirDecision(method, None, None, None, None, False, "unavailable", None, None,
            None, None, 0, ("flat_signal",), _readonly(waveform), _readonly([]), _readonly([]),
            _readonly([]), _readonly(np.asarray(feature_indices)[selected]), feature_kind)
    estimate = rate.estimate(spectrum_features, waveform, fs)
    if not np.isfinite(estimate.bpm) or not np.isfinite(estimate.sharpness):
        raise ValueError("backup estimator returned a nonfinite candidate or sharpness")
    reasons = []
    if estimate.sharpness <= legacy_quality.SHARPNESS_THRESHOLD:
        reasons.append("insufficient_backup_sharpness")
    if not np.isfinite(estimate.agreement) or estimate.agreement >= 3:
        reasons.append("spectral_peak_count_disagreement")
    accepted = bool(estimate.confident and estimate.bpm > 0)
    return PhaseCirDecision(method, float(estimate.bpm), float(estimate.spectral_bpm),
        float(estimate.peak_count_bpm), float(estimate.bpm) if accepted else None, accepted,
        estimate.method, float(estimate.sharpness), _finite(estimate.agreement), _finite(estimate.pm),
        _finite(estimate.detail["band_snr"]), estimate.n_peaks, tuple(reasons), _readonly(waveform),
        _readonly(estimate.detail["freqs"]), _readonly(estimate.detail["power"]),
        _readonly(rate.whiten(estimate.detail["power"])),
        _readonly(np.asarray(feature_indices)[selected]), feature_kind)


def run_phase_cir(
    source: SessionData, config: PhaseCirConfig = PhaseCirConfig(), *, include_legacy: bool = False,
) -> PhaseCirResult:
    """Shared backup preprocessing, then independently computed phase/CIR gates.

Preserves the backup's 114-column compressed IFFT for reproducibility. This is
a legacy CIR representation, not a verified physical delay profile: the three
null bins are omitted and the original code does not restore a full FFT grid.
All IIR filtering retains the backup's full-record edge padding, untrimmed;
the returned times are explicit and no assertion of clean edge support is made.
"""
    if not isinstance(config, PhaseCirConfig):
        raise TypeError("expected PhaseCirConfig")
    block, rows, max_gap = adapt_session(source, config)
    corrected = phase.correct(gain.correct(block, config.gain_method), config.phase_method)
    uniform, fs = resample.to_uniform(corrected)
    if not np.isfinite(uniform.H).all():
        raise ValueError("nonfinite corrected CSI")
    right = np.minimum(np.searchsorted(block.t, uniform.t), len(block.t) - 1)
    left = np.maximum(right - 1, 0)
    nearest = np.minimum(abs(uniform.t - block.t[right]), abs(uniform.t - block.t[left]))
    support = PhaseCirSupport(source, _readonly(rows), _readonly(layout.HT40.take), _readonly(block.f),
        _readonly(corrected.H), _readonly(uniform.t), _readonly(uniform.H), _readonly(nearest > 1e-9),
        float(fs), max_gap, float(fs / len(uniform.t)), config.layout_profile)
    dynamic = cir.remove_static(uniform.H)
    phase_decision = decide_features("phase", dynamic, dynamic, fs, config.top_columns,
                                     layout.HT40.take, "original_buffer_column")
    taps = cir.dynamic_taps(uniform, config.n_taps)
    cir_decision = decide_features("cir", taps, taps, fs, config.top_columns,
                                   np.arange(taps.shape[1]), "legacy_ifft_tap")
    legacy = decide_features("legacy_combined", dynamic, taps, fs, config.top_columns,
                              np.arange(taps.shape[1]), "legacy_ifft_tap") if include_legacy else None
    return PhaseCirResult(config, support, phase_decision, cir_decision, legacy)
