"""Group the legacy comparison output by operator-entered capture conditions.

The legacy report groups sessions by its own directory-name regex, which these
session ids do not match, so its distance tables are empty by construction. This
script re-groups the same rows using the `distance_cm`/`occupancy` values the
operator typed at capture time, copied verbatim through the adapter.

Those values are context, not verified propagation geometry and not ground
truth: nothing here scores an estimate, because no manual breath counts exist.
Empty rooms are reported as empty-room candidate observations, never as 0 bpm.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("amplitude", "phase", "cir")

# Reporting order fixed in run_spec.json: the original far interest first.
GROUP_ORDER = ("100cm", "200cm", "300cm", "500cm", "30cm", "60cm",
               "empty_complete", "empty_operator_interrupted")


def group_of(conditions: dict, interrupted: bool) -> str:
    if str(conditions.get("occupancy")) == "0":
        return "empty_operator_interrupted" if interrupted else "empty_complete"
    distance = conditions.get("distance_cm")
    return "unknown" if distance is None else f"{int(float(distance))}cm"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="outputs/legacy_run_<id>/")
    args = parser.parse_args(argv)
    run = args.run.resolve()
    derived, report = run / "derived", run / "report"

    with (report / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    context: dict[str, dict] = {}
    for path in sorted(derived.iterdir()):
        info = json.loads((path / "session.json").read_text(encoding="utf-8"))
        conditions = info["derived_from"]["source_conditions"] or {}
        annotations = info.get("annotations") or {}
        interrupted = bool(annotations.get("operator_interrupted"))
        context[path.name] = {
            "group": group_of(conditions, interrupted),
            "occupancy": conditions.get("occupancy"),
            "distance_cm": conditions.get("distance_cm"),
            "posture": conditions.get("posture"),
            "operator_interrupted": interrupted,
            "n_packets": info["n_kept"],
            "n_excluded_rows": info["excluded_rows"]["n"],
            "timestamp_policy": info["timestamp_policy"]["policy"],
            "source_status": info["derived_from"]["source_status"],
            "host_elapsed_s": (info["derived_from"]["source_timing"] or {}).get("elapsed_host_s"),
        }

    detail = []
    for row in rows:
        meta = context[row["session_id"]]
        detail.append({
            "group": meta["group"],
            "session_id": row["session_id"],
            "method": row["method"],
            "status": row["status"],
            "diagnostic_bpm": row["diagnostic_bpm"],
            "candidate_bpm": row["candidate_bpm"],
            "spectral_bpm": row["spectral_bpm"],
            "peak_count_bpm": row["peak_count_bpm"],
            "concentration": row["concentration"],
            "acf_peak": row["acf_peak"],
            "amplitude_local_snr": row["amplitude_local_snr"],
            "sharpness": row["sharpness"],
            "agreement_bpm": row["agreement_bpm"],
            "backup_band_snr": row["backup_band_snr"],
            "rejection_reasons": row["reasons"],
            "original_duration_s": row["original_duration_s"],
            "analysis_duration_s": row["analysis_duration_s"],
            "resolution_bpm": row["resolution_bpm"],
            "n_used_raw_packets": row["n_used_raw_packets"],
            "native_rate_hz": row["native_rate_hz"],
            "occupancy": meta["occupancy"],
            "distance_cm": meta["distance_cm"],
            "operator_interrupted": meta["operator_interrupted"],
            "timestamp_policy": meta["timestamp_policy"],
            "n_excluded_rows": meta["n_excluded_rows"],
        })
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    detail.sort(key=lambda r: (order.get(r["group"], 99), r["session_id"],
                               METHODS.index(r["method"]) if r["method"] in METHODS else 9))

    counts = []
    for group in GROUP_ORDER:
        sessions = sorted({r["session_id"] for r in detail if r["group"] == group})
        if not sessions:
            continue
        entry = {"group": group, "n_sessions": len(sessions), "session_ids": sessions}
        for method in METHODS:
            subset = [r for r in detail if r["group"] == group and r["method"] == method]
            entry[method] = {
                "n": len(subset),
                "diagnostic_available": sum(bool(r["diagnostic_bpm"]) for r in subset),
                "accepted": sum(r["status"] == "accepted" for r in subset),
                "withheld": sum(r["status"] == "withheld" for r in subset),
                "unavailable": sum(r["status"] == "unavailable" for r in subset),
                "accepted_bpm": [float(r["candidate_bpm"]) for r in subset if r["candidate_bpm"]],
                "diagnostic_bpm": [float(r["diagnostic_bpm"]) for r in subset if r["diagnostic_bpm"]],
            }
        counts.append(entry)

    with (run / "grouped_summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(detail[0]))
        writer.writeheader()
        writer.writerows(detail)
    (run / "grouped_summary.json").write_text(json.dumps({
        "note": ("Groups come from operator-entered conditions, not verified geometry. "
                 "No manual breath counts exist, so no estimate here is scored. "
                 "Empty-room rows are candidate/false-positive observations, not 0 bpm labels."),
        "group_order": list(GROUP_ORDER),
        "groups": counts,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    for entry in counts:
        line = " ".join(f"{m}:acc={entry[m]['accepted']}/{entry[m]['n']}" for m in METHODS)
        print(f"{entry['group']:<28} n={entry['n_sessions']} {line}")
    print("Wrote", run / "grouped_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
