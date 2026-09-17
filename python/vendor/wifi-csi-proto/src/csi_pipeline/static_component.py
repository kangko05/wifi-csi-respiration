"""Offline complex centering; not identified direct-path or RF cancellation.

Optional common-phase alignment uses one recorded reference vector. It cannot
correct subcarrier-dependent phase errors and can remove shared physical motion.
Means and projection axes use the whole session, including future observations.
No labels, filtering, interpolation, gain compensation or time compression.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .amplitude import AmplitudeSeries
from .input import SessionData


def _freeze(array):
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class StaticComponentConfig:
    align_common_phase: bool = False
    min_alignment_coherence: float = 1e-8

    def __post_init__(self):
        if type(self.align_common_phase) is not bool:
            raise TypeError("align_common_phase must be boolean")
        value = self.min_alignment_coherence
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("min_alignment_coherence must be numeric")
        if not np.isfinite(value) or not 0 < value <= 1:
            raise ValueError("min_alignment_coherence must be in (0, 1]")


@dataclass(frozen=True)
class StaticComponentResult:
    source: SessionData
    config: StaticComponentConfig
    reference_row_index: int | None
    phase_rotation: NDArray[np.complex128]
    alignment_coherence: NDArray[np.float64]
    valid_mask: NDArray[np.bool_]
    static_mean: NDArray[np.complex128]
    residual_csi: NDArray[np.complex128]
    mean_energy_fraction: NDArray[np.float64]
    projection_axis: NDArray[np.complex128]
    projection_energy_fraction: NDArray[np.float64]
    control: AmplitudeSeries
    magnitude: AmplitudeSeries
    projection: AmplitudeSeries


def remove_static_component(
    source: SessionData, config: StaticComponentConfig = StaticComponentConfig(),
) -> StaticComponentResult:
    """Subtract each column's valid-observation complex mean.

The phase reference is the earliest row with the most valid nonzero columns.
Each row is aligned using only its intersection with that reference's valid
columns. Undefined/near-orthogonal alignment invalidates the row at its original
time. Coherence is a numerical alignment diagnostic, not respiration evidence.

The signed output projects each centered I/Q trajectory on its own maximum-
variance axis. This avoids mandatory absolute-value folding but does not
identify a breathing axis. Magnitude is supplied separately for comparison.
"""
    if not isinstance(source, SessionData):
        raise TypeError("expected raw SessionData")
    if not isinstance(config, StaticComponentConfig):
        raise TypeError("expected StaticComponentConfig")
    original = source.valid_sample_mask
    if not np.isfinite(source.csi[original]).all():
        raise ValueError("valid CSI must be finite")
    csi = np.where(original, source.csi, 0).astype(np.complex128)
    mask = original.copy()
    n_rows, n_bins = csi.shape
    rotation = np.ones(n_rows, dtype=np.complex128)
    coherence = np.full(n_rows, np.nan)
    reference_row = None
    if config.align_common_phase:
        counts = (mask & (csi != 0)).sum(axis=1)
        if counts.max(initial=0) == 0:
            mask[:] = False
            rotation[:] = np.nan
        else:
            reference_row = int(np.argmax(counts))
            reference = csi[reference_row]
            shared = mask & mask[reference_row] & (reference != 0)
            dot = (np.where(shared, csi, 0) * reference.conj()).sum(axis=1)
            energy = (np.where(shared, np.abs(csi) ** 2, 0).sum(axis=1)
                      * np.where(shared, np.abs(reference) ** 2, 0).sum(axis=1))
            norm = np.sqrt(energy)
            coherence = np.divide(np.abs(dot), norm, out=np.zeros(n_rows), where=norm > 0)
            np.clip(coherence, 0, 1, out=coherence)
            usable = (norm > 0) & (coherence >= config.min_alignment_coherence)
            rotation[:] = np.nan
            rotation[usable] = dot[usable].conj() / np.abs(dot[usable])
            mask &= usable[:, None]
            csi *= np.where(usable, rotation, 1)[:, None]
    counts = mask.sum(axis=0)
    mean = np.divide(np.where(mask, csi, 0).sum(axis=0), counts,
                     out=np.full(n_bins, np.nan + 0j), where=counts > 0)
    residual = np.where(mask, csi - mean, np.nan + 0j)
    average_power = np.divide(np.where(mask, np.abs(csi) ** 2, 0).sum(axis=0), counts,
                              out=np.full(n_bins, np.nan), where=counts > 0)
    fraction = np.divide(np.abs(mean) ** 2, average_power,
                         out=np.full(n_bins, np.nan), where=average_power > 0)
    axis = np.full(n_bins, np.nan + 0j)
    axis_fraction = np.full(n_bins, np.nan)
    projected = np.full(csi.shape, np.nan)
    for column in np.flatnonzero(counts):
        values = residual[mask[:, column], column]
        xy = np.column_stack((values.real, values.imag))
        eigenvalues, vectors = np.linalg.eigh(xy.T @ xy / len(xy))
        direction = vectors[:, -1]
        if direction[np.argmax(np.abs(direction))] < 0:
            direction = -direction
        axis[column] = direction[0] + 1j * direction[1]
        total = eigenvalues.sum()
        if total > 0:
            axis_fraction[column] = float(np.clip(eigenvalues[-1] / total, 0, 1))
        projected[mask[:, column], column] = xy @ direction
    return StaticComponentResult(
        source, config, reference_row, _freeze(rotation), _freeze(coherence), _freeze(mask),
        _freeze(mean), _freeze(residual), _freeze(fraction), _freeze(axis), _freeze(axis_fraction),
        AmplitudeSeries(source, "static_control", np.abs(csi), mask, "whole_session"),
        AmplitudeSeries(source, "static_magnitude", np.abs(residual), mask, "whole_session"),
        AmplitudeSeries(source, "static_projection", projected, mask, "whole_session"),
    )
