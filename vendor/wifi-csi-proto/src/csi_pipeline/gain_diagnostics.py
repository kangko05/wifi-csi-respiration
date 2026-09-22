"""Offline comparison and plots for stage 2; no respiration decision or tuning.

Matplotlib is loaded only for figures. Statistics use packet observations at
their original timestamps and valid positions, without interpolation.
"""

from __future__ import annotations

import numpy as np

from .gain import GainConfig, GainResult, correct_gain
from .input import SessionData

COLORS = {"none": "#2563eb", "firmware": "#ea580c"}


def packet_rms(csi: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """RMS amplitude over usable bins; no usable bins gives NaN, not zero."""
    count = mask.sum(axis=1)
    power = np.sum(np.where(mask, np.abs(csi) ** 2, 0.0), axis=1)
    return np.sqrt(np.divide(power, count, out=np.full(len(count), np.nan), where=count > 0))


def select_bin(session: SessionData, bin_index: int | None = None) -> int:
    """Select by buffer position only, never by a reference respiration count."""
    usable = session.bin_indices[session.valid_bin_mask]
    if not len(usable):
        raise ValueError("no usable CSI bins to plot")
    if bin_index is None:
        return int(usable[len(usable) // 2])
    if bin_index not in usable:
        raise ValueError(f"raw bin {bin_index} is absent or has no usable observations")
    return int(bin_index)


def _rms_statistics(values: np.ndarray) -> dict:
    finite = values[np.isfinite(values)]
    mean = float(np.mean(finite)) if len(finite) else None
    cv = float(np.std(finite) / mean) if mean is not None and mean > 0 else None
    # Keep original adjacency: NaN packets must not connect distant neighbors.
    steps = np.abs(np.diff(values))
    steps = steps[np.isfinite(steps)]
    relative_step = (
        float(np.median(steps) / mean)
        if mean is not None and mean > 0 and len(steps) else None
    )
    return {"rms_mean": mean, "rms_cv": cv, "relative_step_median": relative_step}


def summarize_comparison(none: GainResult, firmware: GainResult) -> dict:
    if none.source is not firmware.source:
        raise ValueError("comparison paths must share the same raw SessionData")
    if none.config.method != "none" or firmware.config.method != "firmware":
        raise ValueError("comparison requires none and firmware paths, in that order")
    source = none.source
    gain = firmware.applied_gain
    agc, fft = source.metadata["agc_gain"], source.metadata["fft_gain"]
    row = {
        "session_id": source.session_id,
        "n_packets": len(source.time_s),
        "n_valid_bins": int(source.valid_bin_mask.sum()),
        "duration_s": float(source.time_s[-1] - source.time_s[0]),
        "recorded_gain_min": float(gain.min()),
        "recorded_gain_max": float(gain.max()),
        "n_factor_changes": int(np.count_nonzero(np.diff(gain))),
        "n_gain_state_changes": int(np.count_nonzero((np.diff(agc) != 0) | (np.diff(fft) != 0))),
        "firmware_readiness": "not_recorded",
    }
    for result in (none, firmware):
        stats = _rms_statistics(packet_rms(result.csi, source.valid_sample_mask))
        row.update({f"{result.config.method}_{key}": value for key, value in stats.items()})
    return row


def compare_session(session: SessionData, bin_index: int | None = None):
    """Return summary and figure for the same two alternatives starting from raw."""
    none = correct_gain(session)
    firmware = correct_gain(session, GainConfig("firmware"))
    selected = select_bin(session, bin_index)
    row = summarize_comparison(none, firmware)
    row["selected_raw_bin"] = selected
    return row, plot_comparison(none, firmware, selected)


def plot_comparison(none: GainResult, firmware: GainResult, bin_index: int):
    import matplotlib.pyplot as plt

    row = summarize_comparison(none, firmware)
    source = none.source
    selected = select_bin(source, bin_index)
    column = int(np.flatnonzero(source.bin_indices == selected)[0])
    time_s, mask = source.time_s, source.valid_sample_mask
    amplitude = [np.where(mask, np.abs(result.csi), np.nan) for result in (none, firmware)]
    fig, axes = plt.subplots(
        6, 1, figsize=(13, 15), sharex=True,
        gridspec_kw={"height_ratios": [0.8, 0.8, 1.15, 1.15, 1.5, 1.5]},
    )
    fig.subplots_adjust(left=0.09, right=0.88, top=0.92, bottom=0.07, hspace=0.28)
    condition = " | unoccupied room" if "null" in source.session_id.lower() else ""
    fig.suptitle(f"{source.session_id}{condition}", fontsize=15, y=0.973)
    fig.text(
        0.09, 0.947,
        f"Stage 2: gain only | {row['n_packets']} packets | {row['duration_s']:.3f} s | "
        f"raw buffer bin {selected} (position-based selection)",
        fontsize=10,
    )
    axes[0].step(time_s, firmware.applied_gain, where="post", color="#7c3aed", lw=0.7)
    axes[0].set_ylabel("Recorded\ncoefficient")
    for name, color in (("agc_gain", "#047857"), ("fft_gain", "#6b7280")):
        axes[1].step(time_s, source.metadata[name], where="post", label=name, color=color, lw=0.6)
    axes[1].set_ylabel("Gain state\n(metadata)")
    axes[1].legend(loc="upper right", ncol=2)
    for result, amp in zip((none, firmware), amplitude, strict=True):
        name = result.config.method
        axes[2].plot(
            time_s, packet_rms(result.csi, mask), color=COLORS[name],
            label=name, lw=0.65, alpha=0.85, rasterized=True,
        )
        axes[3].plot(
            time_s, amp[:, column], color=COLORS[name],
            label=name, lw=0.65, alpha=0.85, rasterized=True,
        )
    axes[2].set_ylabel("Packet RMS\n(arbitrary units)")
    axes[3].set_ylabel(f"Bin {selected} amplitude\n(arbitrary units)")
    axes[2].legend(loc="upper right", ncol=2)
    for ax in axes[2:4]:
        ax.set_ylim(bottom=0)
    # Cells use actual timestamp midpoints; this is display geometry, not resampling.
    edges = np.r_[time_s[0], (time_s[1:] + time_s[:-1]) / 2, time_s[-1]]
    bin_edges = np.r_[source.bin_indices - 0.5, source.bin_indices[-1] + 0.5]
    vmax = max(float(np.nanmax(amp)) for amp in amplitude)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#d1d5db")
    for ax, amp, name in zip(axes[4:], amplitude, ("none", "firmware"), strict=True):
        mesh = ax.pcolormesh(
            edges, bin_edges, amp.T, vmin=0, vmax=max(vmax, 1e-12),
            cmap=cmap, shading="flat", rasterized=True,
        )
        ax.set_ylabel(f"{name}\nraw buffer bin")
    color_axis = fig.add_axes([0.90, axes[5].get_position().y0, 0.015,
                               axes[4].get_position().y1 - axes[5].get_position().y0])
    fig.colorbar(mesh, cax=color_axis, label="Amplitude (shared scale within session)")
    for ax in axes[:4]:
        ax.grid(alpha=0.18)
    axes[-1].set_xlim(time_s[0], time_s[-1])
    axes[-1].set_xlabel("Time from first aligned packet (s)")
    fig.text(
        0.09, 0.025,
        "No filtering, resampling, or respiration estimate. Gray = masked input. "
        "Heatmap cells span timestamp midpoints.\n"
        "Correction readiness was not recorded. Gain baseline can differ across captures; "
        "absolute amplitudes are not cross-session calibrated.",
        fontsize=9, color="#374151",
    )
    return fig


def plot_overview(records: list[dict]):
    import matplotlib.pyplot as plt

    labels = [
        row["session_id"] + (" *" if "null" in row["session_id"].lower() else "")
        for row in records
    ]
    y = np.arange(len(records))
    fig, axes = plt.subplots(1, 2, figsize=(14, max(7, len(records) * 0.31 + 2)),
                             gridspec_kw={"width_ratios": [1.6, 1]}, sharey=True)
    fig.subplots_adjust(left=0.32, right=0.97, top=0.88, bottom=0.14, wspace=0.15)
    for method, offset in (("none", -0.15), ("firmware", 0.15)):
        values = [
            row[f"{method}_rms_cv"] * 100 if row[f"{method}_rms_cv"] is not None else np.nan
            for row in records
        ]
        axes[0].barh(y + offset, values, height=0.28, label=method, color=COLORS[method])
    axes[0].set_yticks(y, labels, fontsize=9)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Packet RMS coefficient of variation (%)")
    axes[0].legend(loc="lower right")
    for index, row in enumerate(records):
        axes[1].plot([row["recorded_gain_min"], row["recorded_gain_max"]], [index, index],
                     "o-", color="#7c3aed", lw=1.5, ms=3)
    axes[1].axvline(1, color="#9ca3af", lw=0.8, ls="--")
    axes[1].set_xlabel("Recorded coefficient: min to max")
    for ax in axes:
        ax.grid(axis="x", alpha=0.2)
        ax.set_axisbelow(True)
    fig.suptitle("Gain comparison across all loaded sessions", fontsize=16, y=0.965)
    fig.text(0.32, 0.925, "Both paths start from identical raw CSI. * = unoccupied room.", fontsize=10)
    fig.text(
        0.32, 0.045,
        "CV = standard deviation / mean, computed over valid packet RMS observations.\n"
        "Smaller CV is not proof of better respiration sensing. These are development diagnostics.\n"
        "No reference breath counts were used. Firmware baseline readiness is unknown.",
        fontsize=9, color="#374151",
    )
    return fig
