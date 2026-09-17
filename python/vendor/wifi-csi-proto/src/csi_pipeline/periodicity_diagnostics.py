"""Focused evidence plots and post-estimation reference comparisons."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from .periodicity import PeriodicityResult, summarize_periodicity


def load_references(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    references = {}
    for row in rows:
        name = row["session_id"]
        if name in references:
            raise ValueError(f"duplicate reference: {name}")
        if row["group"] not in ("counted", "uncertain", "movement", "empty"):
            raise ValueError(f"unknown reference group: {name}")
        low, high = row["count_low"], row["count_high"]
        if row["group"] == "empty":
            if low or high:
                raise ValueError("empty rooms must not have numeric breath counts")
            low = high = None
        else:
            low, high = float(low), float(high)
            if not np.isfinite(low) or not np.isfinite(high) or not 0 < low <= high:
                raise ValueError(f"invalid count range: {name}")
        references[name] = dict(group=row["group"], low=low, high=high, note=row.get("note", ""))
    return references


def evaluate_reference(summary: dict, duration_s: float, reference: dict | None) -> dict:
    """Join whole-record counts only; no fallback to strongest rejected peak."""
    output = dict(
        reference_group=reference["group"] if reference else "unavailable",
        reference_bpm_low=None, reference_bpm_high=None,
        absolute_error_to_reference_range_bpm=None,
        empty_room_periodic_candidate=None,
        reference_note=reference["note"] if reference else "",
    )
    candidate = summary["candidate_bpm"]
    if reference and reference["group"] == "empty":
        output["empty_room_periodic_candidate"] = candidate is not None
    elif reference:
        low, high = reference["low"] * 60 / duration_s, reference["high"] * 60 / duration_s
        output.update(reference_bpm_low=low, reference_bpm_high=high)
        if candidate is not None:
            output["absolute_error_to_reference_range_bpm"] = max(low - candidate, candidate - high, 0.0)
    return output


def evaluation_totals(rows: list[dict]) -> dict:
    totals = {}
    for group in ("counted", "uncertain", "movement", "empty", "unavailable"):
        selected = [row for row in rows if row["reference_group"] == group]
        errors = [row["absolute_error_to_reference_range_bpm"] for row in selected
                  if row["absolute_error_to_reference_range_bpm"] is not None]
        totals[group] = {
            "n_sessions": len(selected),
            "n_with_candidate": sum(row["candidate_bpm"] is not None for row in selected),
            "mae_among_returned_candidates_bpm": float(np.mean(errors)) if errors else None,
            "median_absolute_error_among_returned_candidates_bpm": float(np.median(errors)) if errors else None,
        }
    return totals


def plot_periodicity(result: PeriodicityResult):
    """No manual reference is passed to plot or candidate selection."""
    import matplotlib.pyplot as plt

    full = result.full_session
    fig, axes = plt.subplots(4, 1, figsize=(11, 10), layout="constrained")
    session_id = result.source.source.source.session_id
    fig.suptitle(f"{session_id} | amplitude periodicity | {result.amplitude.method}", fontsize=13)
    index = full.selected_index if full.selected_index is not None else full.best_index
    if index is not None:
        candidate = full.candidates[index]
        state = "periodic candidate" if full.selected else "strongest unsupported attempt"
        axes[0].plot(full.time_s, full.processed_amplitude[:, index], lw=0.8, color="#2563eb")
        axes[0].set_title(f"{state} | raw bin {candidate.bin_index} | PSD {candidate.psd_hz * 60:.2f} bpm",
                          fontsize=10, loc="left")
        axes[1].plot(full.frequencies_hz * 60, full.psd[:, index], lw=1, color="#2563eb")
        axes[1].axvline(candidate.psd_hz * 60, color="#ea580c", ls="--")
        axes[1].set_xlim(0, result.config.max_hz * 60 + 6)
        axes[2].plot(full.lags_s, full.acf[:, index], color="#059669", lw=1)
        if candidate.acf_hz:
            axes[2].axvline(1 / candidate.acf_hz, color="#ea580c", ls="--")
        axes[2].set_xlim(0, min(1 / result.config.min_hz, float(full.lags_s[-1]) / 2))
    else:
        axes[0].text(0.5, 0.5, "Unavailable: " + ", ".join(full.reasons),
                     transform=axes[0].transAxes, ha="center")
    axes[0].set_ylabel("Detrended amplitude")
    axes[0].set_xlabel("Original time (s); filter edges removed")
    axes[1].set_ylabel("PSD (amplitude²/Hz)")
    axes[1].set_xlabel("Frequency converted to bpm")
    axes[2].set_ylabel("Normalized ACF")
    axes[2].set_xlabel("Lag (s)")
    records = [summarize_periodicity(w) for w in result.windows]
    x = [(r["start_s"] + r["end_s"]) / 2 for r in records]
    axes[3].scatter(x, [r["best_psd_bpm"] if r["best_psd_bpm"] is not None else np.nan for r in records],
                    s=14, color="#94a3b8", label="strongest attempt")
    axes[3].scatter(x, [r["candidate_bpm"] if r["candidate_bpm"] is not None else np.nan for r in records],
                    s=19, color="#2563eb", label="supported periodic candidate")
    axes[3].set_ylim(0, result.config.max_hz * 60 + 3)
    axes[3].set_xlabel("Window center (s); whole-file counts are not window labels")
    axes[3].set_ylabel("Candidate bpm")
    axes[3].legend(loc="upper right", fontsize=8)
    for ax in axes:
        ax.grid(alpha=0.18)
    return fig


def save_full_arrays(path: Path, result: PeriodicityResult):
    full = result.full_session
    source = result.source.source.source
    np.savez_compressed(
        path, source_time_s=source.time_s, metadata_row_indices=source.metadata_row_indices,
        source_valid_sample_mask=source.valid_sample_mask, source_bin_indices=source.bin_indices,
        time_s=full.time_s, processed_amplitude=full.processed_amplitude,
        bin_indices=full.bin_indices, frequencies_hz=full.frequencies_hz, psd=full.psd,
        lags_s=full.lags_s, acf=full.acf, input_grid_time_s=full.input_grid_time_s,
        interpolated_mask=full.interpolated_mask,
        amplitude_method=np.array(result.amplitude.method),
        preprocessing_scope=np.array(result.amplitude.preprocessing_scope),
        input_amplitude=result.amplitude.values,
        input_amplitude_mask=result.amplitude.valid_mask,
        full_amplitude_eligible_bin_mask=full.amplitude_support.eligible_bin_mask,
        full_amplitude_bin_valid_fraction=full.amplitude_support.bin_valid_fraction,
        full_amplitude_bin_max_gap_s=full.amplitude_support.bin_max_gap_s,
    )
