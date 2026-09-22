"""B0 recorded-input comparison; preserves every capture and baseline path."""
import ctypes as c
import json
from pathlib import Path
import sys
import time
import test_amplitude_experiments as experiments
from benchmark_amplitude_experiments import load_records
from test_amplitude_raw import ROOT


def main():
    experiments.AmplitudeExperimentTests.setUpClass()
    runner=experiments.AmplitudeExperimentTests()
    rows=[]
    try:
        for path in sorted((ROOT/'data').glob('*/serial.bin')):
            records=load_records(path); origin=records[0].timestamp
            records=[r for r in records if ((r.timestamp-origin)&0xffffffff)<=60000000]
            baseline=None
            for name,mode in [('double',0),('float',1),('q15',2)]:
                t=time.perf_counter(); status,result=runner.run_case(records,single=mode)
                elapsed=time.perf_counter()-t
                if name=='float': baseline=result
                k=result.selected_bin if result.selected_bin>=0 else result.best_bin
                candidate=result.bins[k] if k>=0 else None
                row=dict(session=path.parent.name,variant=name,status=status,
                    records=len(records),host_seconds=elapsed,
                    selected_bin=result.selected_bin,best_bin=result.best_bin,
                    psd_bpm=candidate.psd_bpm if candidate else None,
                    acf_bpm=candidate.acf_bpm if candidate else None,
                    reasons=candidate.reasons if candidate else None)
                if baseline is not None and not status:
                    row.update(acceptance_changed=(baseline.selected_bin>=0)!=(result.selected_bin>=0),
                        best_bin_changed=baseline.best_bin!=result.best_bin,
                        selected_bin_changed=baseline.selected_bin!=result.selected_bin,
                        per_bin_acceptance_changes=sum(result.bins[j].accepted!=baseline.bins[j].accepted for j in range(result.n_bins)),
                        max_score_delta=max(abs(result.bins[j].score-baseline.bins[j].score) for j in range(result.n_bins)),
                        max_psd_bpm_delta=max([abs(result.bins[j].psd_bpm-baseline.bins[j].psd_bpm) for j in range(result.n_bins) if result.bins[j].has_psd and baseline.bins[j].has_psd] or [0]),
                        max_acf_bpm_delta=max([abs(result.bins[j].acf_bpm-baseline.bins[j].acf_bpm) for j in range(result.n_bins) if result.bins[j].has_acf and baseline.bins[j].has_acf] or [0]))
                rows.append(row)
            print(path.parent.name,flush=True)
    finally:
        experiments.AmplitudeExperimentTests.tearDownClass()
    Path(sys.argv[1]).write_text(json.dumps(rows,indent=2)+'\n')

if __name__=='__main__': main()
