"""Run the legacy amplitude/phase/CIR comparison on derived data, writing here.

Why a wrapper exists
--------------------
`../wifi-csi-proto/scripts/compare_phase_cir.py` refuses any `--output` that is
not inside the legacy repository's own `outputs/` directory
(`compare_phase_cir.py:159-163`). The legacy repository is read-only for this
work, so the script cannot be invoked directly with an output path in this
project. This wrapper reproduces that script's `main()` orchestration and
changes exactly one thing: the output location guard now requires a new,
empty directory under *this* project's `outputs/`.

Everything that computes or reports is the unchanged legacy code, imported from
the legacy checkout: `load_session`, `correct_gain`, `assess_quality`,
`estimate_periodicity`, `column_evidence`, `run_phase_cir`, and the legacy
`blank_row`, `evaluate_rows`, `totals`, `plot_session`, `write_report`,
`write_csv`, `save_json`, `fingerprints`. All thresholds and configurations are
the legacy defaults; nothing is tuned here. Amplitude acceptance comes from
`estimate_periodicity` (gain `none`, 0.05-0.8 Hz, concentration 0.35, ACF 0.3,
PSD/ACF agreement, duration/cycle and band-edge checks). The `EvidenceConfig`
local SNR (threshold 3) is only computed and reported as `amplitude_local_snr`;
it is not an acceptance gate here. Phase/CIR acceptance is sharpness > 1.55 and
agreement < 3 bpm.

Legacy location (2026-09-17): the legacy code is imported from the byte-identical
vendored snapshot `vendor/wifi-csi-proto/` (source commit and file hashes in its
`VENDOR.json`), so a clone of this repository runs without the sibling checkout.
The `legacy_git` manifest entry therefore records the vendored provenance instead
of querying a git checkout.

Post-run note (2026-09-17): this paragraph was clarified after the
`legacy_run_20260917` execution (comment-only). That run's manifest keeps the
execution-time wrapper SHA256 `0a4fa4f7...eaca`; the current file hash differs.

The recorded difference, the legacy commit and the SHA256 of every legacy source
file used are written into the run manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LEGACY = (ROOT / "vendor" / "wifi-csi-proto").resolve()

WRAPPER_DIFFERENCE = (
    "compare_phase_cir.main() requires --output under the legacy repository's outputs/ "
    "(compare_phase_cir.py:159-163). The legacy checkout is read-only for this run, so this "
    "wrapper repeats main()'s orchestration with that guard pointing at this project's "
    "outputs/ instead. Legacy code is imported from the byte-identical vendored snapshot "
    "vendor/wifi-csi-proto (see VENDOR.json). No legacy file was modified, and every estimator, threshold, report "
    "and provenance check is the unchanged legacy code."
)


def _legacy_commit() -> dict:
    # The vendored snapshot is not its own git checkout; report its recorded provenance and
    # whether every vendored file still matches the hash recorded when it was copied.
    try:
        vendor = json.loads((LEGACY / "VENDOR.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover - environment
        return {"commit": None, "error": str(exc)}
    modified = [name for name, digest in vendor["sha256"].items()
                if not (LEGACY / name).is_file()
                or hashlib.sha256((LEGACY / name).read_bytes()).hexdigest() != digest]
    return {
        "commit": vendor["source_git_commit"],
        "vendored": True,
        "vendored_files_match_recorded_sha256": not modified,
        "vendored_files_modified_or_missing": modified,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="directory of derived legacy-format sessions")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--methods", nargs="+", choices=("amplitude", "phase", "cir"),
                        default=["amplitude", "phase", "cir"])
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--reference-csv", type=Path,
                        help="Evaluation only; never an estimator input")
    args = parser.parse_args(argv)

    if not LEGACY.is_dir():
        parser.error(f"vendored legacy snapshot not found at {LEGACY}")
    sys.path.insert(0, str(LEGACY / "src"))
    sys.path.insert(0, str(LEGACY / "scripts"))

    from csi_pipeline import QualityConfig, assess_quality, correct_gain, load_session
    from csi_pipeline.periodicity import PeriodicityConfig, estimate_periodicity
    from csi_pipeline.periodicity_diagnostics import load_references
    from csi_pipeline.phase_cir import PhaseCirConfig, PhaseCirUnavailable, run_phase_cir
    from csi_pipeline.spectral_evidence import EvidenceConfig, column_evidence
    from assess_quality import FILENAMES, fingerprints, write_csv
    from evaluate_band_limits import save_json
    import compare_phase_cir as legacy

    source = args.path.resolve()
    if not source.is_dir():
        parser.error("input must be a session or parent directory")
    output = args.output.resolve()
    # The only behavioural difference from the legacy script, see WRAPPER_DIFFERENCE.
    if not output.is_relative_to(ROOT / "outputs") or output == ROOT / "outputs" \
            or output.is_relative_to(source):
        parser.error("output must be a new directory under this project's outputs, outside input")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("nonempty output exists; use a new directory")
    if args.reference_csv and args.reference_csv.resolve().is_relative_to(output):
        parser.error("reference must be outside output")

    paths = ([source] if any((source / n).exists() for n in FILENAMES)
             else sorted(p for p in source.iterdir() if p.is_dir()))
    if not paths:
        parser.error("no sessions")
    methods = list(dict.fromkeys(args.methods)) + (["legacy_combined"] if args.include_legacy else [])
    inputs = [p / n for p in paths for n in FILENAMES]
    references = [args.reference_csv.resolve()] if args.reference_csv else []
    code = [LEGACY / "scripts/compare_phase_cir.py", LEGACY / "scripts/assess_quality.py",
            LEGACY / "scripts/evaluate_band_limits.py", LEGACY / "main.py",
            LEGACY / "references/phase_cir_backup.json",
            LEGACY / "references/phase_cir_integration.md",
            *sorted((LEGACY / "src/csi_pipeline").rglob("*.py"))]
    hashes = dict(input_sha256=fingerprints(inputs), reference_sha256=fingerprints(references),
                  code_sha256=fingerprints(code))
    config, amp_config = PhaseCirConfig(), PeriodicityConfig()
    manifest = dict(
        status="in_progress", artifact_kind="independent-phase-cir-comparison-v1",
        started_at_utc=datetime.now(timezone.utc).isoformat(), methods=methods,
        phase_cir_config=asdict(config), amplitude_config=asdict(amp_config),
        quality_config=asdict(QualityConfig(window_s=None)),
        amplitude_gain_method="none", evidence_config=asdict(EvidenceConfig()),
        labels_used_in_estimation=False, thresholds_tuned=False,
        phase_cir_gate={"sharpness_strictly_above": 1.55, "agreement_strictly_below_bpm": 3.,
                        "cir_gate_validated": False},
        versions={name: version(name) for name in ("numpy", "scipy", "matplotlib")},
        wrapper=dict(
            wrapper_path=str(Path(__file__).resolve()),
            wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            legacy_root=str(LEGACY), legacy_git=_legacy_commit(),
            difference_from_legacy_script=WRAPPER_DIFFERENCE,
            python_version=sys.version),
        **hashes)
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "manifest.json", manifest)
    import matplotlib
    matplotlib.use("Agg")
    from threadpoolctl import threadpool_limits
    rows = []
    with threadpool_limits(limits=1):
        for path in paths:
            session = load_session(path)
            arrays = dict(source_time_s=session.time_s, source_bin_indices=session.bin_indices,
                          source_metadata_row_indices=session.metadata_row_indices,
                          source_valid_sample_mask=session.valid_sample_mask)
            plots = {}
            if "amplitude" in methods:
                result = estimate_periodicity(
                    assess_quality(correct_gain(session), QualityConfig(window_s=None)), amp_config)
                full = result.full_session
                best = full.best
                row = legacy.blank_row(session, "amplitude")
                evidence = column_evidence(full, amp_config, EvidenceConfig())
                row.update(
                    status="accepted" if full.selected else ("withheld" if best else "unavailable"),
                    diagnostic_bpm=best.psd_hz * 60 if best else None,
                    spectral_bpm=best.psd_hz * 60 if best else None,
                    candidate_bpm=full.selected.psd_hz * 60 if full.selected else None,
                    accepted=full.selected is not None, estimate_method="amplitude_psd_acf",
                    concentration=best.concentration if best else None,
                    acf_peak=best.acf_peak if best else None,
                    amplitude_local_snr=float(evidence.local_snr[full.best_index])
                    if best and np.isfinite(evidence.local_snr[full.best_index]) else None,
                    analysis_start_s=float(full.time_s[0]) if len(full.time_s) else None,
                    analysis_end_s=float(full.time_s[-1]) if len(full.time_s) else None,
                    analysis_duration_s=float(np.ptp(full.time_s)) if len(full.time_s) else None,
                    resolution_bpm=full.resolution_hz * 60 if full.resolution_hz else None,
                    search_grid_step_bpm=float(np.diff(full.frequencies_hz)[0] * 60)
                    if len(full.frequencies_hz) > 1 else None,
                    n_used_raw_packets=int(session.valid_packet_mask.sum()),
                    n_used_raw_columns=len(full.bin_indices), n_selected_features=1 if best else 0,
                    selected_feature_indices=str(best.bin_index) if best else "",
                    feature_kind="original_buffer_column", filter_edge_policy="FIR edges excluded",
                    reasons="|".join(best.reasons if best else full.reasons))
                rows.append(row)
                wave = full.processed_amplitude[:, full.best_index] if best else np.empty(0)
                power = full.psd[:, full.best_index] if best else np.zeros(len(full.frequencies_hz))
                plots["amplitude"] = (full.time_s, wave, full.frequencies_hz,
                                      power / max(float(power.max(initial=0)), 1e-30),
                                      "normalized diagnostic PSD")
                arrays.update(amplitude_time_s=full.time_s, amplitude_waveform=wave,
                              amplitude_frequencies_hz=full.frequencies_hz, amplitude_power=power,
                              amplitude_processed_bin_indices=full.bin_indices,
                              amplitude_input_grid_time_s=full.input_grid_time_s,
                              amplitude_interpolated_mask=full.interpolated_mask)
            requested = [m for m in methods if m != "amplitude"]
            if requested:
                try:
                    result = run_phase_cir(session, config, include_legacy=args.include_legacy)
                except PhaseCirUnavailable as exc:
                    for method in requested:
                        row = legacy.blank_row(session, method)
                        row["reasons"] = str(exc)
                        rows.append(row)
                        plots[method] = (np.empty(0), np.empty(0), np.empty(0), np.empty(0),
                                         "unavailable")
                else:
                    support = result.support
                    arrays.update(
                        phase_cir_time_s=support.time_s,
                        phase_cir_original_row_indices=support.original_row_indices,
                        phase_cir_original_bin_indices=support.original_bin_indices,
                        phase_cir_assumed_frequency_offsets_hz=support.assumed_frequency_offsets_hz,
                        phase_cir_interpolated_time_mask=support.interpolated_time_mask)
                    for method in requested:
                        decision = getattr(result, method)
                        row = legacy.blank_row(session, method)
                        row.update(
                            status="accepted" if decision.accepted
                            else ("withheld" if decision.diagnostic_bpm else "unavailable"),
                            diagnostic_bpm=decision.diagnostic_bpm,
                            candidate_bpm=decision.accepted_bpm, spectral_bpm=decision.spectral_bpm,
                            peak_count_bpm=decision.peak_count_bpm,
                            estimate_method=decision.estimate_method, accepted=decision.accepted,
                            sharpness=decision.sharpness, agreement_bpm=decision.agreement_bpm,
                            backup_band_snr=decision.band_snr,
                            analysis_start_s=float(support.time_s[0]),
                            analysis_end_s=float(support.time_s[-1]),
                            analysis_duration_s=float(np.ptp(support.time_s)),
                            resolution_bpm=support.resolution_hz * 60, search_grid_step_bpm=.3,
                            n_used_raw_packets=len(support.original_row_indices),
                            n_used_raw_columns=len(support.original_bin_indices),
                            n_selected_features=len(decision.selected_feature_indices),
                            selected_feature_indices="|".join(
                                map(str, decision.selected_feature_indices)),
                            feature_kind=decision.feature_kind,
                            layout_profile=support.layout_profile,
                            filter_edge_policy="legacy IIR padding retained; untrimmed",
                            reasons="|".join(decision.reasons))
                        rows.append(row)
                        plots[method] = (support.time_s, decision.waveform, decision.frequencies_hz,
                                         decision.whitened_power, "whitened spectral score")
                        arrays.update({method + "_" + key: value for key, value in dict(
                            waveform=decision.waveform, frequencies_hz=decision.frequencies_hz,
                            power=decision.spectral_power, whitened_power=decision.whitened_power,
                            selected_feature_indices=decision.selected_feature_indices).items()})
            np.savez_compressed(output / f"{path.name}.npz", **arrays)
            legacy.plot_session(output / f"{path.name}.png", path.name, plots)
            print(path.name + ": " + ", ".join(
                f"{r['method']}={r['diagnostic_bpm']} ({r['status']})"
                for r in rows if r["session_id"] == path.name), flush=True)
    legacy.evaluate_rows(rows, load_references(references[0]) if references else {})
    write_csv(output / "summary.csv", rows, list(rows[0]))
    far = [r for r in rows if r["reference_group"] == "counted"
           and r["distance_group"] == "far_90_100cm"]
    evaluation = dict(
        far_counted=legacy.totals(far, methods),
        all_counted=legacy.totals([r for r in rows if r["reference_group"] == "counted"], methods),
        cohorts={group: legacy.totals([r for r in rows if r["reference_group"] == group], methods)
                 for group in ("empty", "uncertain", "movement", "unavailable")})
    save_json(output / "evaluation.json", evaluation)
    legacy.write_report(output, rows, methods)
    for key, files in (("input_sha256", inputs), ("reference_sha256", references),
                       ("code_sha256", code)):
        if fingerprints(files) != hashes[key]:
            raise RuntimeError(key + " changed during run")
    manifest.update(status="complete", completed_at_utc=datetime.now(timezone.utc).isoformat(),
                    n_sessions=len(paths), n_rows=len(rows))
    save_json(output / "manifest.json", manifest)
    print("Far:", json.dumps(evaluation["far_counted"], ensure_ascii=False))
    print("Report:", output / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
