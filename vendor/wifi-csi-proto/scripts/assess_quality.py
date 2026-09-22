"""Assess original-time windows and generate stage 3 development diagnostics."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from csi_pipeline import GainConfig, QualityConfig, assess_quality, correct_gain, load_session
from csi_pipeline.quality_diagnostics import (
    plot_quality, save_arrays, session_record, window_record, write_html,
)

ARTIFACT_KIND = "wifi-csi-quality-assessment-v1"
FILENAMES = ("csi_raw.npy", "meta.csv", "session.json")


def fingerprints(paths):
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("outputs/quality_assessment"))
    parser.add_argument("--gain-method", choices=("none", "firmware"), default="none")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--window-s", type=float, default=30.0)
    window.add_argument("--full-session", action="store_true")
    parser.add_argument("--step-s", type=float, default=1.0)
    parser.add_argument("--max-gap-s", type=float, default=0.25)
    parser.add_argument("--min-valid-fraction", type=float, default=0.9)
    parser.add_argument("--min-packets", type=int, default=2)
    parser.add_argument("--interval-warning-factor", type=float, default=1.5)
    args = parser.parse_args()
    try:
        config = QualityConfig(
            window_s=None if args.full_session else args.window_s, step_s=args.step_s,
            max_gap_s=args.max_gap_s, min_valid_fraction=args.min_valid_fraction,
            min_packets=args.min_packets, interval_warning_factor=args.interval_warning_factor,
        )
    except ValueError as exc:
        parser.error(str(exc))
    source, output = args.path.resolve(), args.output.resolve()
    repository = Path(__file__).resolve().parents[1]
    if not source.is_dir():
        parser.error(f"not a directory: {source}")
    if output.is_relative_to(source) or output.is_relative_to(repository / "data"):
        parser.error("output must be outside raw data directories")
    paths = (
        [source] if any((source / name).exists() for name in FILENAMES)
        else sorted(path for path in source.iterdir() if path.is_dir())
    )
    if not paths:
        parser.error("no session directories found")
    if output.exists():
        if not output.is_dir():
            parser.error("output must be a directory")
        if any(output.iterdir()):
            marker = output / "manifest.json"
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            if not isinstance(previous, dict) or previous.get("artifact_kind") != ARTIFACT_KIND:
                parser.error("output is nonempty and is not a previous quality report")
    inputs = [path / name for path in paths for name in FILENAMES]
    before = fingerprints(inputs)
    results = []
    for path in paths:
        session = load_session(path)
        result = assess_quality(correct_gain(session, GainConfig(args.gain_method)), config)
        results.append(result)
        row = session_record(result)
        print(
            f"{session.session_id}: {row['n_data_usable_windows']}/{row['n_windows']} data-usable windows; "
            f"max gap {1000 * row['max_observation_gap_s']:.3f} ms; "
            f"gain transitions {row['n_gain_transitions']}", flush=True,
        )
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    output.mkdir(parents=True, exist_ok=True)
    # A rerun that fails after writing must not retain a successful old manifest.
    marker = output / "manifest.json"
    if marker.exists():
        marker.write_text(json.dumps({"artifact_kind": ARTIFACT_KIND, "status": "in_progress"}), encoding="utf-8")
    for result in results:
        session_id = result.source.source.session_id
        figure = plot_quality(result)
        figure.savefig(output / f"{session_id}.png", dpi=120)
        plt.close(figure)
        save_arrays(output / f"{session_id}.npz", result)
    records = [session_record(result) for result in results]
    windows = [
        window_record(result, window, index)
        for result in results for index, window in enumerate(result.windows)
    ]
    fields = list(window_record(results[0], results[0].full_session, 0))
    write_csv(output / "summary.csv", records, list(records[0]))
    write_csv(output / "windows.csv", windows, fields)
    write_html(output, results)
    if fingerprints(inputs) != before:
        raise RuntimeError("input files changed during analysis")
    code = [repository / "pyproject.toml", Path(__file__),
            *sorted((repository / "src/csi_pipeline").glob("*.py"))]
    manifest = {
        "artifact_kind": ARTIFACT_KIND, "status": "complete",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command_arguments": sys.argv[1:], "quality_config": asdict(config),
        "gain_method": args.gain_method,
        "versions": {"python": platform.python_version(), "numpy": np.__version__,
                     "matplotlib": matplotlib.__version__},
        "input_sha256": before, "code_sha256": fingerprints(code),
        "n_sessions": len(results), "n_windows": len(windows),
        "n_data_usable_windows": sum(w["data_usable"] for w in windows),
        "reference_counts_used": False, "resampled": False, "filtered": False,
        "respiration_estimated": False, "movement_classifier_calibrated": False,
        "thresholds": "provisional data-usability settings, not fitted to respiration labels",
        "window_convention": "[start, end); full-session includes final packet; no padded tail",
        "event_convention": "assigned to later packet; may reference previous row outside window",
        "nominal_interval_basis": "whole-session median, offline only",
        "valid_fraction_denominator": "captured rows and session-valid bins, not transmission count",
    }
    marker.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Report: {output / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
