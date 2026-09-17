"""Stage 3: offline time windows and data usability, before signal filtering.

Eligibility only describes available observations. It is neither a breathing
decision nor a calibrated motion detector. Raw and gain-corrected diagnostics
share original timestamps and masks; no values are interpolated or discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import numpy as np
from numpy.typing import NDArray

from .gain import GainResult


@dataclass(frozen=True)
class QualityConfig:
    """Provisional data checks, not thresholds fitted to respiration labels.

    None selects the entire capture. Sliding windows are complete, half-open
    intervals [start, end); the separate full-session assessment includes the
    last packet. Tail fragments are not padded or emitted as complete windows.
    max_gap_s covers packet/valid-bin gaps including the window's edges.
    min_valid_fraction uses captured rows, not an inferred transmitted count.
    """

    window_s: float | None = 30.0
    step_s: float = 1.0
    max_gap_s: float = 0.25
    min_valid_fraction: float = 0.9
    min_packets: int = 2
    interval_warning_factor: float = 1.5

    def __post_init__(self) -> None:
        for name in ("window_s", "step_s", "max_gap_s"):
            value = getattr(self, name)
            if name == "window_s" and value is None:
                continue
            if (
                isinstance(value, (bool, np.bool_))
                or not isinstance(value, (int, float))
                or not np.isfinite(value) or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        fraction = self.min_valid_fraction
        if (
            isinstance(fraction, (bool, np.bool_))
            or not isinstance(fraction, (int, float))
            or not np.isfinite(fraction) or not 0 < fraction <= 1
        ):
            raise ValueError("min_valid_fraction must be in (0, 1]")
        if type(self.min_packets) is not int or self.min_packets < 2:
            raise ValueError("min_packets must be an integer >= 2")
        if (
            isinstance(self.interval_warning_factor, (bool, np.bool_))
            or not isinstance(self.interval_warning_factor, (int, float))
            or not np.isfinite(self.interval_warning_factor)
            or self.interval_warning_factor <= 1
        ):
            raise ValueError("interval_warning_factor must be finite and > 1")
        if self.window_s is not None and self.step_s > self.window_s:
            raise ValueError("step_s must not exceed window_s")


@dataclass(frozen=True)
class WindowQuality:
    """Original row slice plus bin eligibility; original masks remain required."""

    start_s: float
    end_s: float
    start_index: int
    stop_index: int
    includes_end: bool
    data_usable: bool
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    eligible_bin_mask: NDArray[np.bool_]
    bin_valid_fraction: NDArray[np.float64]
    bin_max_valid_gap_s: NDArray[np.float64]
    metrics: Mapping[str, int | float | None]

    @property
    def row_slice(self) -> slice:
        return slice(self.start_index, self.stop_index)


@dataclass(frozen=True)
class QualityResult:
    source: GainResult
    config: QualityConfig
    windows: tuple[WindowQuality, ...]
    full_session: WindowQuality
    nominal_interval_s: float
    raw_rms: NDArray[np.float64]
    corrected_rms: NDArray[np.float64]
    raw_step_db: NDArray[np.float64]
    corrected_step_db: NDArray[np.float64]
    gain_transition: NDArray[np.bool_]
    coefficient_change: NDArray[np.bool_]


def _readonly(array: NDArray) -> NDArray:
    array.setflags(write=False)
    return array


def _rms(csi: NDArray, mask: NDArray) -> NDArray:
    count = mask.sum(axis=1)
    power = np.sum(np.where(mask, np.abs(csi) ** 2, 0.0), axis=1)
    return np.sqrt(np.divide(
        power, count, out=np.full(len(count), np.nan), where=count > 0,
    ))


def _steps(csi: NDArray, mask: NDArray, time_s: NDArray, max_gap_s: float) -> NDArray:
    """Signed dB steps on common bins of original adjacent rows.

    Attribute each event to its later packet, even at a window boundary.
    Never connect across an invalid row or a gap longer than max_gap_s.
    """
    common = mask[1:] & mask[:-1]
    before, after = _rms(csi[:-1], common), _rms(csi[1:], common)
    valid = (
        (before > 0) & (after > 0)
        & np.isfinite(before) & np.isfinite(after)
        & (np.diff(time_s) <= max_gap_s)
    )
    result = np.full(len(time_s), np.nan)
    indices = np.flatnonzero(valid) + 1
    result[indices] = 20 * (np.log10(after[valid]) - np.log10(before[valid]))
    return result


def _max_gap(times: NDArray, start: float, end: float) -> float:
    """Longest interval without an observation, including boundary margins."""
    return float(np.max(np.diff(np.r_[start, times, end])))


def _signal_metrics(csi: NDArray, mask: NDArray, rms: NDArray, steps: NDArray) -> dict:
    finite = rms[np.isfinite(rms)]
    mean = float(np.mean(finite)) if finite.size else None
    finite_steps = np.abs(steps[np.isfinite(steps)])
    # Per-bin spread also reveals changes that cancel in aggregate packet RMS.
    bins = mask.sum(axis=0) >= 2
    spread = np.array([], dtype=float)
    if np.any(bins):
        amplitude = np.where(mask[:, bins], np.abs(csi[:, bins]), np.nan)
        q05, median, q95 = np.nanpercentile(amplitude, [5, 50, 95], axis=0)
        positive = median > 0
        spread = (q95[positive] - q05[positive]) / median[positive]
    return {
        "rms_mean": mean,
        "rms_cv": float(np.std(finite) / mean) if mean is not None and mean > 0 else None,
        "n_valid_steps": int(finite_steps.size),
        "abs_step_db_p99": float(np.percentile(finite_steps, 99)) if finite_steps.size else None,
        "abs_step_db_max": float(finite_steps.max()) if finite_steps.size else None,
        "bin_relative_span_median": float(np.median(spread)) if spread.size else None,
        "bin_relative_span_p95": float(np.percentile(spread, 95)) if spread.size else None,
    }


def assess_quality(source: GainResult, config: QualityConfig = QualityConfig()) -> QualityResult:
    """Assess complete windows and the full capture without modifying input.

    Arrays in each window use all original raw-buffer columns. Downstream
    consumers must combine eligible_bin_mask with source.source's sample mask.
    Warnings never make data unusable on their own. Movement thresholds and
    breathing confidence are deliberately not calibrated in this stage.
    """
    if not isinstance(source, GainResult):
        raise TypeError("assess_quality expects GainResult")
    if not isinstance(config, QualityConfig):
        raise TypeError("config must be QualityConfig")
    session = source.source
    time_s = session.time_s
    if len(time_s) < 2 or not np.all(np.isfinite(time_s)) or np.any(np.diff(time_s) <= 0):
        raise ValueError("quality requires at least two strictly increasing finite timestamps")
    if source.csi.shape != session.csi.shape or session.valid_sample_mask.shape != source.csi.shape:
        raise ValueError("CSI and sample masks must preserve the input shape")
    raw = session.csi.astype(np.complex128)
    mask = session.valid_sample_mask & np.isfinite(raw) & np.isfinite(source.csi)
    nominal = float(np.median(np.diff(time_s)))
    raw_rms, corrected_rms = _rms(raw, mask), _rms(source.csi, mask)
    raw_steps = _steps(raw, mask, time_s, config.max_gap_s)
    corrected_steps = _steps(source.csi, mask, time_s, config.max_gap_s)
    gain_transition = np.r_[False, (
        (np.diff(session.metadata["agc_gain"]) != 0)
        | (np.diff(session.metadata["fft_gain"]) != 0)
    )]
    coefficient_change = np.r_[False, np.diff(session.metadata["compensate_gain"]) != 0]

    def assess(start: float, end: float, includes_end: bool = False) -> WindowQuality:
        left = int(np.searchsorted(time_s, start, side="left"))
        right = int(np.searchsorted(time_s, end, side="right" if includes_end else "left"))
        rows = slice(left, right)
        times, local_mask = time_s[rows], mask[rows]
        count = right - left
        bin_counts = local_mask.sum(axis=0)
        bin_fraction = bin_counts / count if count else np.zeros(source.csi.shape[1])
        bin_gap = np.array([
            _max_gap(times[local_mask[:, column]], start, end)
            for column in range(source.csi.shape[1])
        ])
        eligible = (
            session.valid_bin_mask & (bin_counts >= config.min_packets)
            & (bin_fraction >= config.min_valid_fraction)
            & (bin_gap <= config.max_gap_s)
        )
        valid_packets = np.any(local_mask, axis=1)
        valid_count = int(valid_packets.sum())
        packet_fraction = valid_count / count if count else 0.0
        max_gap = _max_gap(times, start, end)
        intervals = np.diff(times)
        n_session_bins = int(session.valid_bin_mask.sum())
        reasons, warnings = [], []
        if count < config.min_packets:
            reasons.append("too_few_packets")
        if valid_count < config.min_packets:
            reasons.append("too_few_valid_packets")
        if packet_fraction < config.min_valid_fraction:
            reasons.append("low_valid_packet_fraction")
        if max_gap > config.max_gap_s:
            reasons.append("long_observation_gap")
        if not np.any(eligible):
            reasons.append("no_eligible_bins")
        if np.any(gain_transition[rows]):
            warnings.append("gain_transition")
        if np.any(coefficient_change[rows]):
            warnings.append("recorded_coefficient_change")
        if count and valid_count < count:
            warnings.append("invalid_packets")
        if np.any(bin_counts[session.valid_bin_mask] < count):
            warnings.append("masked_observations")
        if intervals.size and np.any(intervals > config.interval_warning_factor * nominal):
            warnings.append("irregular_packet_spacing")
        metrics = {
            "n_packets": count,
            "n_valid_packets": valid_count,
            "valid_packet_fraction": packet_fraction,
            "n_session_valid_bins": n_session_bins,
            "n_eligible_bins": int(eligible.sum()),
            "eligible_bin_fraction": float(eligible.sum() / n_session_bins) if n_session_bins else 0.0,
            "valid_observation_fraction": (
                float(local_mask.sum() / (count * n_session_bins))
                if count and n_session_bins else 0.0
            ),
            "observed_span_s": float(times[-1] - times[0]) if count >= 2 else 0.0,
            "interval_min_s": float(intervals.min()) if intervals.size else None,
            "interval_median_s": float(np.median(intervals)) if intervals.size else None,
            "interval_p99_s": float(np.percentile(intervals, 99)) if intervals.size else None,
            "max_observation_gap_s": max_gap,
            "n_intervals_over_warning_limit": int(np.count_nonzero(
                intervals > config.interval_warning_factor * nominal,
            )),
            "n_gain_transitions": int(gain_transition[rows].sum()),
            "n_coefficient_changes": int(coefficient_change[rows].sum()),
        }
        for prefix, csi, rms, steps in (
            ("raw", raw, raw_rms, raw_steps),
            ("corrected", source.csi, corrected_rms, corrected_steps),
        ):
            metrics.update({
                f"{prefix}_{key}": value
                for key, value in _signal_metrics(
                    csi[rows], local_mask, rms[rows], steps[rows],
                ).items()
            })
            values = np.abs(steps[rows][gain_transition[rows]])
            values = values[np.isfinite(values)]
            metrics[f"{prefix}_abs_step_at_gain_change_db_max"] = (
                float(values.max()) if values.size else None
            )
        return WindowQuality(
            start, end, left, right, includes_end, not reasons,
            tuple(reasons), tuple(warnings), _readonly(eligible),
            _readonly(bin_fraction), _readonly(bin_gap), MappingProxyType(metrics),
        )

    start, end = float(time_s[0]), float(time_s[-1])
    full = assess(start, end, includes_end=True)
    if config.window_s is None:
        windows = (full,)
    else:
        # Timestamps are microsecond-derived floats; tolerate only arithmetic
        # roundoff, never a meaningfully incomplete requested window.
        tolerance = 1e-10 * max(1.0, end - start)
        span = end - start - config.window_s
        count = max(0, int(np.floor((span + tolerance) / config.step_s)) + 1)
        windows = tuple(
            assess(start + i * config.step_s, min(end, start + i * config.step_s + config.window_s))
            for i in range(count)
        )
    return QualityResult(
        source, config, windows, full, nominal,
        *map(_readonly, (raw_rms, corrected_rms, raw_steps, corrected_steps,
                        gain_transition, coefficient_change)),
    )
