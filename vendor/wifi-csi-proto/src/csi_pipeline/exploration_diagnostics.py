"""Common descriptive evaluation for experiments; never changes decisions."""

from __future__ import annotations

import re

import numpy as np


def distribution(values):
    finite = np.asarray([v for v in values if v is not None], dtype=float)
    if not np.isfinite(finite).all():
        raise ValueError("distribution values must be finite or None")
    return dict(n=len(finite), **dict(zip(
        ("min", "q25", "median", "q75", "max"),
        map(float, np.quantile(finite, [0, .25, .5, .75, 1])) if len(finite) else [None] * 5,
    )))


def collect_diagnostics(result, method=None):
    """Always report the diagnostic best column, separately from acceptance."""
    full = result.full_session
    session = result.source.source.source
    best = full.best
    total = full.psd[full.frequencies_hz > 0].sum(axis=0)
    high = full.psd[full.frequencies_hz > .8].sum(axis=0)
    valid = total > 0
    high_ratio = high[valid] / total[valid]
    return dict(
        session_id=session.session_id, method=method or result.amplitude.method,
        original_duration_s=float(np.ptp(session.time_s)),
        native_rate_hz=float(1 / result.source.nominal_interval_s),
        best_psd_bpm=best.psd_hz * 60 if best and best.psd_hz else None,
        best_bin=best.bin_index if best else None,
        best_acf_bpm=best.acf_hz * 60 if best and best.acf_hz else None,
        best_acf_peak=best.acf_peak if best else None,
        best_concentration=best.concentration if best else None,
        best_score=best.score if best else None,
        candidate_bpm=full.selected.psd_hz * 60 if full.selected else None,
        resolution_bpm=full.resolution_hz * 60 if full.resolution_hz else None,
        n_analyzed_bins=len(full.bin_indices), n_nonflat_bins=len(high_ratio),
        high_frequency_power_ratio_median=float(np.median(high_ratio)) if len(high_ratio) else None,
        analysis_duration_s=float(np.ptp(full.time_s)) if len(full.time_s) else 0.,
        filter_edge_trim_s=full.filter_edge_trim_s,
        retained_fraction_of_input=full.amplitude_support.retained_fraction_of_input,
    )


def join_references(rows, references):
    """Post-estimation only. Whole-file midpoint errors; no window labels."""
    for row in rows:
        ref = references.get(row["session_id"])
        row.update(reference_group=ref["group"] if ref else "unavailable", reference_bpm=None,
                   best_error_bpm=None, within_one_resolution_bin=None, within_two_resolution_bins=None,
                   within_1_1_bpm=None, within_2_2_bpm=None, harmonic_like=None)
        if not ref or ref["group"] == "empty":
            continue
        target = (ref["low"] + ref["high"]) / 2 * 60 / row["original_duration_s"]
        row["reference_bpm"] = target
        if row["best_psd_bpm"] is None:
            continue
        estimate, tolerance = row["best_psd_bpm"], row["resolution_bpm"]
        error = abs(estimate - target)
        row.update(best_error_bpm=error, within_1_1_bpm=error <= 1.1, within_2_2_bpm=error <= 2.2)
        if tolerance is not None:
            harmonic = []
            if error > tolerance:
                if abs(estimate - 2 * target) <= tolerance:
                    harmonic.append("double")
                if abs(estimate - target / 2) <= tolerance:
                    harmonic.append("half")
            row.update(within_one_resolution_bin=error <= tolerance,
                       within_two_resolution_bins=error <= 2 * tolerance,
                       harmonic_like="|".join(harmonic) or "none")


def common_totals(rows):
    output = {}
    for method in sorted({r["method"] for r in rows}):
        groups = {}
        for group in ("counted", "uncertain", "movement", "empty", "unavailable"):
            subset = [r for r in rows if r["method"] == method and r["reference_group"] == group]
            groups[group] = dict(
                n_sessions=len(subset), n_with_best=sum(r["best_psd_bpm"] is not None for r in subset),
                n_accepted=sum(r["candidate_bpm"] is not None for r in subset),
                best_error_bpm=distribution([r["best_error_bpm"] for r in subset]),
                n_within_one_resolution_bin=sum(r["within_one_resolution_bin"] is True for r in subset),
                n_within_two_resolution_bins=sum(r["within_two_resolution_bins"] is True for r in subset),
                n_within_1_1_bpm=sum(r["within_1_1_bpm"] is True for r in subset),
                n_within_2_2_bpm=sum(r["within_2_2_bpm"] is True for r in subset),
                n_double_like=sum(r["harmonic_like"] == "double" for r in subset),
                n_half_like=sum(r["harmonic_like"] == "half" for r in subset),
                evidence={name: distribution([r[name] for r in subset]) for name in (
                    "best_concentration", "best_acf_peak", "best_score", "resolution_bpm",
                    "high_frequency_power_ratio_median",
                )},
            )
        output[method] = groups
    return output


def add_evaluation_groups(rows):
    """Attach nominal distance labels and measured cadence, after estimation.

    Directory labels are not verified propagation geometry. Empty rooms have no
    person-to-sensor distance even if the capture directory contains a distance.
    """
    for row in rows:
        name = row["session_id"]
        distance = None
        match = re.match(r"^(?:emptyroom_case|mr)_(50cm|75cm|90cm|1m)(?:_|$)", name)
        if match:
            distance = {"50cm": 50, "75cm": 75, "90cm": 90, "1m": 100}[match[1]]
        else:
            match = re.match(r"^mr2_(30|60|90)(?:_|$)", name)
            if match:
                distance = int(match[1])
        if row["reference_group"] == "empty":
            group = "not_applicable_empty"
        elif distance is None:
            group = "unknown"
        else:
            group = "near_30_50cm" if distance <= 50 else ("middle_60_75cm" if distance <= 75 else "far_90_100cm")
        rate = row.get("native_rate_hz")
        sampling = "unknown"
        if rate is not None and np.isfinite(rate):
            if abs(rate - 50) <= 2.5:
                sampling = "50Hz"
            elif abs(rate - 100) <= 5:
                sampling = "100Hz"
        row.update(distance_cm_label=distance, distance_group=group, sampling_rate_group=sampling)


def grouped_totals(rows):
    """Preserve reference cohorts inside distance, cadence and joint groups."""
    output = {}
    for name, fields in (("distance", ("distance_group",)), ("sampling_rate", ("sampling_rate_group",)),
                         ("distance_and_sampling_rate", ("distance_group", "sampling_rate_group"))):
        groups = {}
        keys = sorted({tuple(r[field] for field in fields) for r in rows})
        for key in keys:
            subset = [r for r in rows if tuple(r[field] for field in fields) == key]
            groups["/".join(key)] = common_totals(subset)
        output[name] = groups
    return output


def flatten_grouped_totals(grouped):
    rows = []
    for dimension, groups in grouped.items():
        for group_name, methods in groups.items():
            for method, cohorts in methods.items():
                for cohort, result in cohorts.items():
                    if not result["n_sessions"]:
                        continue
                    rows.append(dict(
                        dimension=dimension, subgroup=group_name, method=method, reference_group=cohort,
                        n_sessions=result["n_sessions"], n_with_best=result["n_with_best"],
                        median_error_bpm=result["best_error_bpm"]["median"],
                        n_within_one_resolution_bin=result["n_within_one_resolution_bin"],
                        n_within_two_resolution_bins=result["n_within_two_resolution_bins"],
                        n_within_1_1_bpm=result["n_within_1_1_bpm"],
                        n_within_2_2_bpm=result["n_within_2_2_bpm"], n_accepted=result["n_accepted"],
                        high_frequency_power_ratio_median=result["evidence"]["high_frequency_power_ratio_median"]["median"],
                    ))
    return rows


def high_frequency_summary(rows):
    raw = [r for r in rows if r["method"] == "none"]
    subset = [r for r in raw if r["session_id"].startswith("mr") or r["reference_group"] == "empty"]
    return {
        "all_sessions": distribution([r["high_frequency_power_ratio_median"] for r in raw]),
        "mr_and_empty": distribution([r["high_frequency_power_ratio_median"] for r in subset]),
    }


def permutation_check(rows, *, method="none", n_resamples=100_000, seed=0,
                      tolerance_bpm=1.1, strata_field=None):
    """Shuffle converted reference bpm assignments; predictions stay fixed.

    This exploratory association check is conditional on the chosen estimator,
    dataset and tolerance. It is not a breathing-accuracy validation. A stratified
    run exchanges answers only within that group, using the same statistic.
    """
    if type(n_resamples) is not int or n_resamples < 1:
        raise ValueError("n_resamples must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if not np.isfinite(tolerance_bpm) or tolerance_bpm <= 0:
        raise ValueError("tolerance_bpm must be finite and positive")
    selected = sorted((r for r in rows if r["method"] == method and r["reference_group"] == "counted"),
                      key=lambda r: r["session_id"])
    ids = [r["session_id"] for r in selected]
    if len(ids) < 2 or len(ids) != len(set(ids)):
        raise ValueError("permutation check needs at least two unique counted sessions")
    predicted = np.array([r["best_psd_bpm"] if r["best_psd_bpm"] is not None else np.nan for r in selected])
    answers = np.array([r["reference_bpm"] for r in selected], dtype=float)
    if not np.isfinite(answers).all() or np.isinf(predicted).any():
        raise ValueError("reference bpm must be finite; predictions must be finite or absent")
    labels = [str(r[strata_field]) for r in selected] if strata_field else ["all"] * len(selected)
    blocks = {label: np.array([i for i, key in enumerate(labels) if key == label]) for label in sorted(set(labels))}
    exact_mean = sum(float(np.count_nonzero(np.abs(predicted[ix, None] - answers[None, ix]) <= tolerance_bpm)) / len(ix)
                     for ix in blocks.values())
    observed = int(np.count_nonzero(np.abs(predicted - answers) <= tolerance_bpm))
    rng = np.random.default_rng(seed)
    counts = np.empty(n_resamples, dtype=np.int64)
    shuffled = np.empty_like(answers)
    for i in range(n_resamples):
        for indices in blocks.values():
            shuffled[indices] = rng.permutation(answers[indices])
        counts[i] = np.count_nonzero(np.abs(predicted - shuffled) <= tolerance_bpm)
    extreme = int(np.count_nonzero(counts >= observed))
    histogram = np.bincount(counts, minlength=len(selected) + 1)
    return dict(
        method=method, n_sessions=len(selected), session_order=ids,
        n_missing_predictions=int(np.isnan(predicted).sum()), tolerance_bpm=tolerance_bpm,
        n_resamples=n_resamples, seed=seed, bit_generator=type(rng.bit_generator).__name__,
        strata_field=strata_field, strata_sizes={key: len(ix) for key, ix in blocks.items()},
        observed_matches=observed, exact_null_mean_matches=exact_mean,
        sampled_mean_matches=float(counts.mean()), sampled_max_matches=int(counts.max()),
        n_resamples_at_least_observed=extreme, monte_carlo_p_greater=(extreme + 1) / (n_resamples + 1),
        histogram={str(i): int(count) for i, count in enumerate(histogram)},
        statistic="number of fixed best PSD predictions within tolerance of assigned converted reference bpm",
        interpretation="exploratory random-pairing null; exchangeability assumed within strata; no correction for prior method exploration",
        p_value_convention="(1 + count(shuffled >= observed)) / (1 + n_resamples)",
        source="https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.permutation_test.html",
    )


def pca_gain_diagnostics(session):
    """Recompute the paper's row-centered leading component, no causal claim."""
    packets = np.flatnonzero(session.valid_packet_mask)
    columns = np.flatnonzero(np.all(session.valid_sample_mask[packets], axis=0))
    values = np.abs(session.csi[np.ix_(packets, columns)].astype(np.complex128))
    centered = values - values.mean(axis=1, keepdims=True)
    eigenvalues, eigenvectors = np.linalg.eigh(centered.T @ centered)
    component = centered @ eigenvectors[:, -1]
    gain = np.asarray(session.metadata["compensate_gain"], dtype=float)[packets]
    agc = np.asarray(session.metadata["agc_gain"])[packets]
    fft = np.asarray(session.metadata["fft_gain"])[packets]
    record = dict(session_id=session.session_id, n_valid_packets=len(packets),
                  gain_transitions=int(np.count_nonzero((np.diff(agc) != 0) | (np.diff(fft) != 0))),
                  coefficient_changes=int(np.count_nonzero(np.diff(gain))),
                  rank1_energy_fraction=float(eigenvalues[-1] / np.sum(centered**2)) if np.any(centered) else 0.)
    for field in ("compensate_gain", "agc_gain", "fft_gain", "inverse_compensate_gain"):
        comparison = 1 / gain if field == "inverse_compensate_gain" else np.asarray(session.metadata[field], dtype=float)[packets]
        record[f"abs_r_{field}"] = float(abs(np.corrcoef(component, comparison)[0, 1])) if (
            np.std(component) > 0 and np.std(comparison) > 0
        ) else None
    return record
