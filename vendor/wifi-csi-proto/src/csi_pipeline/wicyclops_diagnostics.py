"""Stage-separated diagnostics for WiCyclops amplitude processing."""

from __future__ import annotations

import numpy as np

from .gain import GainConfig, correct_gain
from .wicyclops import WiCyclopsResult

METHODS = ("none", "firmware", "pca", "density", "smoothed")
COLORS = ("#64748b", "#ea580c", "#2563eb", "#059669", "#7c3aed")


def amplitude_paths(result: WiCyclopsResult) -> dict[str, np.ndarray]:
    source = result.source
    return {
        "none": np.where(source.valid_sample_mask, np.abs(source.csi.astype(np.complex128)), np.nan),
        "firmware": np.where(source.valid_sample_mask,
                             np.abs(correct_gain(source, GainConfig("firmware")).csi), np.nan),
        "pca": result.pca_amplitude,
        "density": result.retained_amplitude,
        "smoothed": result.smoothed_amplitude,
    }


def _cv(values):
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) < 2 or np.mean(values) <= 0:
        return None
    return float(np.std(values) / np.mean(values))


def summarize_wicyclops(result: WiCyclopsResult, bin_index: int | None = None):
    source = result.source
    usable = source.bin_indices[result.pca_mask.any(axis=0)]
    if bin_index is None:
        bin_index = int(usable[len(usable) // 2])
    if bin_index not in usable:
        raise ValueError(f"bin {bin_index} is not complete-case valid for PCA")
    column = int(np.flatnonzero(source.bin_indices == bin_index)[0])
    paths = amplitude_paths(result)
    common = np.logical_and.reduce([np.isfinite(values[:, column]) for values in paths.values()])
    kept = result.retained_mask[:, column]
    supported_times = np.r_[source.time_s[0], source.time_s[kept], source.time_s[-1]]
    eligible = result.pca_mask[:, column]
    row = {
        "session_id": source.session_id,
        "selected_raw_bin": bin_index,
        "n_packets": len(source.time_s),
        "duration_s": float(source.time_s[-1] - source.time_s[0]),
        "n_pca_bins": len(usable),
        "rank1_energy_fraction": result.rank1_energy_fraction,
        "retained_fraction_all_bins": float(result.retained_mask.sum() / result.pca_mask.sum()),
        "retained_fraction_selected_bin": float(kept.sum() / eligible.sum()),
        "common_fraction_selected_bin": float(common.sum() / eligible.sum()),
        "max_retained_spacing_s": float(np.max(np.diff(supported_times))),
        "selected_bin_radius": float(result.radius[column]),
        "n_radius_floored_bins": int(result.radius_floored.sum()),
        "n_bins_without_cluster": int(np.sum(result.selected_cluster[result.pca_mask.any(axis=0)] < 0)),
        "n_negative_pca_values": int(np.sum(result.pca_amplitude[result.pca_mask] < 0)),
    }
    for name, values in paths.items():
        row[f"{name}_cv_common"] = _cv(values[common, column])
        row[f"{name}_cv_available"] = _cv(values[:, column])
    return row, paths


def plot_wicyclops(result: WiCyclopsResult, row: dict, paths: dict):
    import matplotlib.pyplot as plt

    source = result.source
    time_s = source.time_s
    column = int(np.flatnonzero(source.bin_indices == row["selected_raw_bin"])[0])
    fig = plt.figure(figsize=(13, 15))
    grid = fig.add_gridspec(6, 1, height_ratios=[1, 1, 1, 1, .65, 1])
    axes = [fig.add_subplot(grid[0])]
    for index in range(1, 5):
        axes.append(fig.add_subplot(grid[index], sharex=axes[0]))
    axes.append(fig.add_subplot(grid[5]))
    fig.subplots_adjust(left=.10, right=.96, top=.91, bottom=.105, hspace=.40)
    condition = " | unoccupied room" if "null" in source.session_id.lower() else ""
    fig.suptitle(source.session_id + condition, y=.976, fontsize=15)
    fig.text(.10, .948, f"Fixed raw bin {row['selected_raw_bin']} | "
             f"Retained: bin {row['retained_fraction_selected_bin']:.1%}, "
             f"all bins {row['retained_fraction_all_bins']:.1%} | "
             f"Rank-1 centered energy {result.rank1_energy_fraction:.1%}", fontsize=10)
    fig.text(.10, .929, "Separate raw-start paths. PCA/density/smoothing are successive ablations; "
             "they do not use firmware gain.", fontsize=10)
    for name, color in zip(METHODS[:2], COLORS[:2], strict=True):
        axes[0].plot(time_s, paths[name][:, column], label=name, color=color,
                     linewidth=.65, alpha=.8, rasterized=True)
    axes[0].set_ylabel("Amplitude\nraw / firmware")
    pca = result.pca_amplitude[:, column]
    kept = result.retained_mask[:, column]
    rejected = result.pca_mask[:, column] & ~kept
    axes[1].plot(time_s, pca, color=COLORS[2], linewidth=.65, label="PCA", rasterized=True)
    axes[1].scatter(time_s[rejected], pca[rejected], color="#dc2626", s=7,
                    label="excluded by density", rasterized=True)
    axes[1].set_ylabel("PCA amplitude\nand exclusions")
    axes[2].plot(time_s, paths["density"][:, column], color=COLORS[3],
                 linewidth=.6, alpha=.6, label="PCA + density", rasterized=True)
    axes[2].plot(time_s, paths["smoothed"][:, column], color=COLORS[4],
                 linewidth=.9, label=f"+ moving average ({result.config.moving_average_s:g} s)",
                 rasterized=True)
    axes[2].set_ylabel("Retained amplitude\nand smoothing")
    for name, color in zip(METHODS, COLORS, strict=True):
        values = paths[name][:, column]
        finite = values[np.isfinite(values)]
        if len(finite) and np.mean(finite) > 0:
            axes[3].plot(time_s, 100 * (values / np.mean(finite) - 1),
                         label=name, color=color, linewidth=.6, alpha=.75, rasterized=True)
    axes[3].set_ylabel("Relative amplitude\n(% of own mean)")
    denominator = result.pca_mask.sum(axis=1)
    fraction = np.divide(result.retained_mask.sum(axis=1), denominator,
                         out=np.full(len(time_s), np.nan), where=denominator > 0)
    axes[4].plot(time_s, 100 * fraction, color=COLORS[3], linewidth=.7, rasterized=True)
    axes[4].set_ylim(-3, 103)
    axes[4].set_ylabel("Bins retained\nper packet (%)")
    axes[4].set_xlabel("Actual time from first aligned packet (s)")
    limits = np.histogram_bin_edges(pca[np.isfinite(pca)], bins=50)
    axes[5].hist(pca[kept], bins=limits, color=COLORS[3], alpha=.7, label="retained", rasterized=True)
    axes[5].hist(pca[rejected], bins=limits, color="#dc2626", alpha=.7, label="excluded", rasterized=True)
    axes[5].set_ylabel("Observations")
    axes[5].set_xlabel(f"PCA amplitude of bin {row['selected_raw_bin']} "
                       f"| adaptive DBSCAN radius = {row['selected_bin_radius']:.5g}")
    for ax in axes:
        ax.grid(alpha=.15)
    for ax in [*axes[:4], axes[5]]:
        ax.legend(loc="upper right", ncol=3 if ax is axes[3] else 2, fontsize=8)
    for ax in axes[:4]:
        ax.tick_params(labelbottom=False)
    axes[0].set_xlim(time_s[0], time_s[-1])
    fig.text(.10, .026,
             "No respiration rate or presence decision. Excluded observations retain their time positions as NaN.\n"
             "Moving-average width is an explicit adaptation; the paper does not specify it. "
             "Relative centering above is display-only.\n"
             "A smoother trace, high rank-1 energy, or a retained cluster does not establish respiration.",
             fontsize=9, color="#374151")
    return fig


def plot_wicyclops_overview(records):
    import matplotlib.pyplot as plt

    y = np.arange(len(records))
    fig, axes = plt.subplots(1, 2, figsize=(14, max(8, .43 * len(records) + 2)),
                             sharey=True, gridspec_kw={"width_ratios": [1.4, 1]})
    fig.subplots_adjust(left=.33, right=.97, top=.9, bottom=.12, wspace=.13)
    for i, (name, color) in enumerate(zip(METHODS, COLORS, strict=True)):
        values = [100 * row[f"{name}_cv_common"] if row[f"{name}_cv_common"] is not None else np.nan
                  for row in records]
        axes[0].barh(y + (i - 2) * .14, values, height=.13, label=name, color=color)
    labels = [r["session_id"] + (" *" if "null" in r["session_id"].lower() else "") for r in records]
    axes[0].set_yticks(y, labels, fontsize=9)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Fixed-bin amplitude CV (%)\nSame supported timestamps across all paths")
    axes[0].legend(loc="lower right", fontsize=9)
    for offset, key, color, label in [
        (-.16, "retained_fraction_selected_bin", COLORS[3], "selected bin"),
        (.16, "retained_fraction_all_bins", "#94a3b8", "all bins"),
    ]:
        axes[1].barh(y + offset, [100 * r[key] for r in records], height=.28, color=color, label=label)
    axes[1].set_xlim(0, 105)
    axes[1].set_xlabel("Observations retained by density (%)")
    axes[1].legend(loc="lower left", fontsize=9)
    for ax in axes:
        ax.grid(axis="x", alpha=.15)
        ax.set_axisbelow(True)
    fig.suptitle("WiCyclops amplitude-processing comparison", fontsize=16, y=.97)
    fig.text(.33, .934, "* = unoccupied room | lower CV is not respiration accuracy", fontsize=10)
    fig.text(.33, .035,
             "PCA preserves packet means and reconstructs one centered component. Density selection may discard real changes.\n"
             "All CV bars use the same retained timestamps of a position-selected bin; rejected times are not evaluated there.\n"
             "Inspect coverage and session plots together. No breath-count labels or frequency-based selection were used.",
             fontsize=9, color="#374151")
    return fig
