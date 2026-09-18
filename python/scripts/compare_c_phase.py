"""Compare a C phase run with the frozen Python run and current C amplitude.

Only evaluation reads manual counts. No estimator is called with labels.
Run after run_c_amplitude.py --method phase, supplying its output directory.
"""
import argparse
import csv
import json
from pathlib import Path
import statistics
from run_c_amplitude import ROOT, digest

def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_run",type=Path)
    parser.add_argument("--amplitude-run",type=Path,
        default=ROOT/"outputs/c-amplitude/20260918T064726_104124Z")
    args=parser.parse_args()
    output=args.phase_run.resolve()
    if not output.is_relative_to(ROOT/"outputs"):
        parser.error("phase run must be under this repository's outputs")
    if (output/"comparison.csv").exists():
        parser.error("comparison already exists")
    legacy=ROOT/"python/outputs/legacy_run_20260917"
    legacy_path=legacy/"report/summary.csv"
    old={r["session_id"]:r for r in read_csv(legacy_path) if r["method"]=="phase"}
    amp={r["session"]:r for r in read_csv(args.amplitude_run/"summary.csv")}
    refs_path=ROOT/"python/references/manual_breath_counts_20260916.csv"
    refs={r["session_id"]:r for r in read_csv(refs_path)}
    rows=[]
    max_diff={key:0. for key in ("diagnostic_bpm","spectral_bpm","peak_count_bpm","sharpness","agreement_bpm")}
    for entry in read_csv(output/"summary.csv"):
        sid=entry["session"]
        current=json.loads((output/f"{sid}.json").read_text(encoding="utf-8"))
        previous=old[sid]
        derived=json.loads((legacy/"derived"/sid/"session.json").read_text(encoding="utf-8"))
        # The frozen reference's adapter records the raw capture digest.
        if derived["derived_from"]["source_raw_sha256"] != current["source_sha256"]["serial.bin"]:
            raise ValueError(f"different source capture: {sid}")
        if not current["sources_unchanged"] or current["status"]=="process_error":
            raise ValueError(f"C processing failed or input changed: {sid}")
        accepted=bool(current["accepted"])
        matched=accepted==(previous["accepted"]=="True")
        for key in max_diff:
            difference=abs(current[key]-float(previous[key]))
            max_diff[key]=max(max_diff[key],difference)
            matched &= difference < 1e-6
        matched &= current["selected_bins"]==[int(v) for v in previous["selected_feature_indices"].split("|")]
        matched &= current["n_retained"]==int(previous["n_used_raw_packets"])
        matched &= abs(current["duration_s"]-float(previous["analysis_duration_s"]))<1e-8
        ref=refs[sid]; target=float(ref["reference_bpm"]) if ref["reference_bpm"] else None
        amp_bpm=float(amp[sid]["diagnostic_psd_bpm"])
        rows.append({"session":sid,"group":ref["group"],"occupancy":ref["occupancy"],
            "reference_bpm":target,"c_phase_bpm":current["diagnostic_bpm"],
            "c_phase_accepted":accepted,"python_phase_bpm":float(previous["diagnostic_bpm"]),
            "python_parity":bool(matched),"c_amplitude_psd_bpm":amp_bpm,
            "c_amplitude_accepted":bool(amp[sid]["candidate_bpm"]),
            "phase_abs_error":abs(current["diagnostic_bpm"]-target) if target is not None else None,
            "amplitude_abs_error":abs(amp_bpm-target) if target is not None else None,
            "phase_reasons":"|".join(current["reason_names"])})
    with (output/"comparison.csv").open("w",encoding="utf-8-sig",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    occupied=[r for r in rows if r["reference_bpm"] is not None]
    accepted=[r for r in occupied if r["c_phase_accepted"]]
    empty=[r for r in rows if r["occupancy"]=="0"]
    summary={"sessions":len(rows),"python_parity_sessions":sum(r["python_parity"] for r in rows),
        "maximum_python_difference":max_diff,"occupied":len(occupied),"phase_accepted":len(accepted),
        "phase_accepted_mae_bpm":statistics.mean(r["phase_abs_error"] for r in accepted),
        "phase_all_diagnostic_mae_bpm":statistics.mean(r["phase_abs_error"] for r in occupied),
        "amplitude_all_diagnostic_mae_bpm":statistics.mean(r["amplitude_abs_error"] for r in occupied),
        "phase_accepted_within_1_1":sum(r["phase_abs_error"]<=1.1 for r in accepted),
        "phase_accepted_within_2_2":sum(r["phase_abs_error"]<=2.2 for r in accepted),
        "empty_sessions":len(empty),"empty_phase_accepted":sum(r["c_phase_accepted"] for r in empty),
        "empty_amplitude_accepted":sum(r["c_amplitude_accepted"] for r in empty),
        "notes":["Manual counts are unsynchronized full-record references, evaluation only.",
                 "Frozen Python reference reused; numerical stages independently tested.",
                 "Amplitude and phase acceptance rules and analyzed edge support differ.",
                 "Two complete empty captures and one interrupted empty capture are separate cases."],
        "reference_sha256":{str(p):digest(p) for p in [legacy_path,refs_path,args.amplitude_run/"summary.csv"]}}
    (output/"comparison.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2))
    return 0 if all(r["python_parity"] for r in rows) else 1

if __name__=="__main__":
    raise SystemExit(main())
