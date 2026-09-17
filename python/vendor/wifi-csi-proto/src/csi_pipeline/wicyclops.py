"""WiCyclops Sections 5-6: amplitude PCA, density selection, and optional smoothing.

Source: wi-cyclops.pdf pp. 10, 12-14, DOI 10.1145/3632958.
This is an offline adaptation, not the complete respiration estimator.
It starts from raw SessionData and never applies firmware gain coefficients.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .input import SessionData


@dataclass(frozen=True)
class WiCyclopsConfig:
    min_samples: int = 10  # Paper's DBSCAN minPts, including the point itself.
    kmeans_n_init: int = 10  # Implementation choice; paper does not specify.
    random_state: int = 0
    moving_average_s: float = 0.25  # Implementation choice, not a paper value.
    min_smoothing_samples: int = 3

    def __post_init__(self):
        for name in ("min_samples", "kmeans_n_init", "min_smoothing_samples"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.random_state) is not int or not 0 <= self.random_state < 2**32:
            raise ValueError("random_state must be a uint32 integer")
        if not np.isfinite(self.moving_average_s) or self.moving_average_s < 0:
            raise ValueError("moving_average_s must be finite and nonnegative")


@dataclass(frozen=True)
class WiCyclopsResult:
    """Real amplitude arrays [packet, original bin], NaN outside each output mask.

    pca_mask uses bins valid at EVERY input-valid packet in this offline window.
    retained_mask selects the largest DBSCAN cluster independently per bin.
    smoothed_mask also requires local support; rejected centers stay missing.
    Labels: -2 = not processed, -1 = DBSCAN noise, >=0 = density cluster.
    Clusters are NOT respiration labels. No phase is reconstructed.
    """

    source: SessionData
    config: WiCyclopsConfig
    pca_amplitude: NDArray[np.float64]
    retained_amplitude: NDArray[np.float64]
    smoothed_amplitude: NDArray[np.float64]
    pca_mask: NDArray[np.bool_]
    retained_mask: NDArray[np.bool_]
    smoothed_mask: NDArray[np.bool_]
    cluster_labels: NDArray[np.int32]
    radius: NDArray[np.float64]
    radius_floored: NDArray[np.bool_]
    selected_cluster: NDArray[np.int32]
    smoothing_count: NDArray[np.int64]
    rank1_energy_fraction: float


def reconstruct_amplitude(amplitude: np.ndarray) -> tuple[np.ndarray, float]:
    """Paper Eq. 5-8, with input [time, bin]; center ACROSS bins at each time.

    Use the smaller bin covariance. Its leading eigenvector produces the same
    rank-one reconstruction as the paper's time covariance, without an N x N
    allocation. Negative reconstruction values are not silently clipped.
    """
    values = np.asarray(amplitude, dtype=np.float64)
    if values.ndim != 2 or min(values.shape) < 2 or not np.all(np.isfinite(values)):
        raise ValueError("PCA needs a finite matrix with at least two packets and bins")
    means = values.mean(axis=1, keepdims=True)
    centered = values - means
    energy = float(np.sum(centered * centered))
    if energy == 0:
        return np.broadcast_to(means, values.shape).copy(), 0.0
    eigenvalues, vectors = np.linalg.eigh(centered.T @ centered)
    profile = vectors[:, -1]
    reconstructed = (centered @ profile)[:, None] * profile[None, :] + means
    return reconstructed, float(np.clip(eigenvalues[-1] / energy, 0, 1))


def dbscan_amplitude(values: np.ndarray, radius: float, min_samples: int) -> np.ndarray:
    """Exact 1-D DBSCAN neighborhoods, O(N log N) time and O(N) storage.

    Sorted amplitude order resolves ambiguous border points toward the lower
    amplitude cluster, like sklearn DBSCAN fed the sorted observations.
    Avoid storing the potentially quadratic neighborhood lists for dense CSI.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise ValueError("DBSCAN input must be a finite vector")
    if not np.isfinite(radius) or radius <= 0:
        raise ValueError("radius must be finite and positive")
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError("min_samples must be a positive integer")
    labels = np.full(len(values), -1, dtype=np.int32)
    if not len(values):
        return labels
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    left = np.searchsorted(sorted_values, sorted_values - radius, side="left")
    right = np.searchsorted(sorted_values, sorted_values + radius, side="right")
    core_indices = np.flatnonzero(right - left >= min_samples)
    if not len(core_indices):
        return labels
    core_values = sorted_values[core_indices]
    core_labels = np.cumsum(np.r_[0, np.diff(core_values) > radius]).astype(np.int32)
    # Earliest core in the closed epsilon neighborhood determines border ties.
    first_core = np.searchsorted(core_values, sorted_values - radius, side="left")
    available = first_core < len(core_values)
    first_core = np.minimum(first_core, len(core_values) - 1)
    available &= core_values[first_core] <= sorted_values + radius
    sorted_labels = np.full(len(values), -1, dtype=np.int32)
    sorted_labels[available] = core_labels[first_core[available]]
    labels[order] = sorted_labels
    return labels


def adaptive_radius(values: np.ndarray, config: WiCyclopsConfig) -> tuple[float, bool]:
    from sklearn.cluster import KMeans

    if np.ptp(values) == 0:
        estimated = 0.0
    else:
        estimator = KMeans(n_clusters=2, n_init=config.kmeans_n_init,
                           random_state=config.random_state, algorithm="lloyd")
        labels = estimator.fit_predict(values[:, None])
        estimated = min(
            float(np.mean(np.abs(values[labels == label] - estimator.cluster_centers_[label, 0])))
            for label in np.unique(labels)
        )
    # Exact constants/singleton clusters can yield zero in Eq. 10. Use only a
    # numerical floor, record it, and allow DBSCAN to reject everything.
    floor = float(8 * np.spacing(max(float(np.max(np.abs(values))), 1.0)))
    return max(estimated, floor), estimated < floor


def smooth_retained(time_s, amplitude, mask, window_s, min_samples):
    """Centered average in actual seconds; no interpolation or time compression."""
    if window_s == 0:
        return np.where(mask, amplitude, np.nan), mask.copy(), mask.astype(np.int64)
    left = np.searchsorted(time_s, time_s - window_s / 2, side="left")
    right = np.searchsorted(time_s, time_s + window_s / 2, side="right")
    sums = np.vstack([np.zeros(amplitude.shape[1]), np.cumsum(np.where(mask, amplitude, 0), axis=0)])
    counts = np.vstack([np.zeros(amplitude.shape[1], dtype=np.int64),
                        np.cumsum(mask, axis=0, dtype=np.int64)])
    support = counts[right] - counts[left]
    valid = mask & (support >= min_samples)
    result = np.divide(sums[right] - sums[left], support,
                       out=np.full(amplitude.shape, np.nan), where=valid)
    return result, valid, support


def process_wicyclops(
    session: SessionData, config: WiCyclopsConfig = WiCyclopsConfig()
) -> WiCyclopsResult:
    """Process one whole offline window; no labels, respiration band or rate."""
    from threadpoolctl import threadpool_limits

    if not isinstance(session, SessionData):
        raise TypeError("process_wicyclops expects raw SessionData")
    if not isinstance(config, WiCyclopsConfig):
        raise TypeError("config must be WiCyclopsConfig")
    packets = np.flatnonzero(session.valid_packet_mask)
    bins = np.flatnonzero(np.all(session.valid_sample_mask[packets], axis=0)) if len(packets) else []
    if len(packets) < 2 or len(bins) < 2:
        raise ValueError("need at least two valid packets and two complete-case bins")
    shape = session.csi.shape
    pca = np.full(shape, np.nan)
    pca_mask = np.zeros(shape, dtype=bool)
    retained_mask = np.zeros(shape, dtype=bool)
    labels = np.full(shape, -2, dtype=np.int32)
    radius = np.full(shape[1], np.nan)
    floored = np.zeros(shape[1], dtype=bool)
    selected = np.full(shape[1], -1, dtype=np.int32)
    with threadpool_limits(limits=1):
        reconstruction, fraction = reconstruct_amplitude(np.abs(session.csi[np.ix_(packets, bins)].astype(np.complex128)))
        pca[np.ix_(packets, bins)] = reconstruction
        pca_mask[np.ix_(packets, bins)] = True
        for column in bins:
            values = pca[packets, column]
            radius[column], floored[column] = adaptive_radius(values, config)
            bin_labels = dbscan_amplitude(values, radius[column], config.min_samples)
            labels[packets, column] = bin_labels
            clusters, counts = np.unique(bin_labels[bin_labels >= 0], return_counts=True)
            if len(clusters):
                selected[column] = clusters[np.argmax(counts)]
                retained_mask[packets, column] = bin_labels == selected[column]
    retained = np.where(retained_mask, pca, np.nan)
    smoothed, smoothed_mask, support = smooth_retained(
        session.time_s, pca, retained_mask, config.moving_average_s, config.min_smoothing_samples
    )
    for array in (pca, retained, smoothed, pca_mask, retained_mask, smoothed_mask,
                  labels, radius, floored, selected, support):
        array.setflags(write=False)
    return WiCyclopsResult(session, config, pca, retained, smoothed, pca_mask,
                          retained_mask, smoothed_mask, labels, radius, floored,
                          selected, support, fraction)
