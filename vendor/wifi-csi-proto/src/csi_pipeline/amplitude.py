"""Real-valued amplitude inputs with original coordinates and explicit masks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .gain import GainResult
from .input import SessionData
from .quality import QualityResult, WindowQuality
from .wicyclops import WiCyclopsResult


def _readonly(array):
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class AmplitudeSeries:
    """An offline signal, not reconstructed complex CSI.

    Signed PCA values are preserved. Invalid observations remain NaN at their
    original rows. Factories copy arrays so inputs cannot mutate the result.
    """

    source: SessionData
    method: str
    values: NDArray[np.float64]
    valid_mask: NDArray[np.bool_]
    preprocessing_scope: str

    def __post_init__(self):
        if not isinstance(self.source, SessionData):
            raise TypeError("source must be SessionData")
        if self.method not in ("none", "firmware", "pca", "density", "smoothed", "static_control",
                               "static_magnitude", "static_projection"):
            raise ValueError("unknown amplitude method")
        expected_scope = "packet" if self.method in ("none", "firmware") else "whole_session"
        if self.preprocessing_scope != expected_scope:
            raise ValueError("preprocessing scope must match method")
        values, mask = np.asarray(self.values), np.asarray(self.valid_mask)
        if values.shape != self.source.csi.shape or mask.shape != values.shape:
            raise ValueError("amplitude and mask must preserve original rows and columns")
        if np.iscomplexobj(values) or not np.issubdtype(values.dtype, np.number):
            raise TypeError("amplitude must be a real numeric matrix")
        if mask.dtype != np.bool_:
            raise TypeError("valid_mask must be boolean")
        if np.any(mask & ~self.source.valid_sample_mask):
            raise ValueError("preprocessing cannot restore invalid input observations")
        if not np.isfinite(values[mask]).all():
            raise ValueError("valid amplitude observations must be finite")
        object.__setattr__(self, "values", _readonly(np.where(mask, values, np.nan).astype(np.float64)))
        object.__setattr__(self, "valid_mask", _readonly(mask.copy()))


def amplitude_from_gain(result: GainResult) -> AmplitudeSeries:
    if not isinstance(result, GainResult):
        raise TypeError("expected GainResult")
    return AmplitudeSeries(result.source, result.config.method, np.abs(result.csi),
                           result.source.valid_sample_mask & np.isfinite(result.csi), "packet")


def amplitude_from_wicyclops(result: WiCyclopsResult, stage: str = "smoothed") -> AmplitudeSeries:
    if not isinstance(result, WiCyclopsResult):
        raise TypeError("expected WiCyclopsResult from raw SessionData")
    attributes = {
        "pca": ("pca_amplitude", "pca_mask"),
        "density": ("retained_amplitude", "retained_mask"),
        "smoothed": ("smoothed_amplitude", "smoothed_mask"),
    }
    if stage not in attributes:
        raise ValueError("stage must be pca, density or smoothed")
    values, mask = attributes[stage]
    return AmplitudeSeries(result.source, stage, getattr(result, values),
                           getattr(result, mask), "whole_session")


@dataclass(frozen=True)
class AmplitudeSupport:
    """Availability before interpolation, measured separately from raw quality."""

    retained_fraction_of_input: float
    bin_valid_fraction: NDArray[np.float64]
    bin_max_gap_s: NDArray[np.float64]
    retention_eligible_mask: NDArray[np.bool_]
    eligible_bin_mask: NDArray[np.bool_]
    max_gap_among_input_eligible_bins_s: float | None


def assess_amplitude_support(
    series: AmplitudeSeries, quality: QualityResult, window: WindowQuality, max_gap_s: float,
) -> AmplitudeSupport:
    """Reapply unchanged per-bin count/fraction and gap rules after preprocessing.

    Fractions for eligibility use all captured rows, as in stage 3. Retention
    relative to valid raw observations is an additional diagnostic, not a gate.
    Window boundaries contribute to gaps; no rejected time is compressed.
    """
    if series.source is not quality.source.source:
        raise ValueError("amplitude and quality must refer to the same SessionData object")
    mask = series.valid_mask[window.row_slice]
    times = series.source.time_s[window.row_slice]
    original = series.source.valid_sample_mask[window.row_slice]
    count = len(times)
    counts = mask.sum(axis=0)
    fraction = counts / count if count else np.zeros(mask.shape[1])
    gaps = np.array([
        np.max(np.diff(np.r_[window.start_s, times[mask[:, col]], window.end_s]))
        for col in range(mask.shape[1])
    ], dtype=float)
    retention = (
        window.eligible_bin_mask & (counts >= quality.config.min_packets)
        & (fraction >= quality.config.min_valid_fraction)
    )
    eligible = retention & (gaps <= min(max_gap_s, quality.config.max_gap_s))
    original_count = int(original.sum())
    return AmplitudeSupport(
        float(mask.sum() / original_count) if original_count else 0.0,
        _readonly(fraction), _readonly(gaps), _readonly(retention), _readonly(eligible),
        float(gaps[window.eligible_bin_mask].max()) if window.eligible_bin_mask.any() else None,
    )
