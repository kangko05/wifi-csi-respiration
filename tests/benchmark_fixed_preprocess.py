"""Recorded B1 comparison: fixed preprocessing, with/without Q15 FFT."""
import ctypes as c
import json
from pathlib import Path
import sys
import time
import test_amplitude_experiments as exp
from benchmark_amplitude_experiments import load_records
from test_amplitude_raw import Record, ROOT


def main():
    exp.AmplitudeExperimentTests.setUpClass()
    runner=exp.AmplitudeExperimentTests(); rows=[]
    lib=runner.lib
    lib.test_fixed_preprocess_error.argtypes=[c.POINTER(Record),c.c_size_t,
        c.c_float,c.c_float,c.POINTER(c.c_double),c.POINTER(c.c_int)]
    try:
        for path in sorted((ROOT/'data').glob('*/serial.bin')):
            records=load_records(path); origin=records[0].timestamp
            records=[r for r in records if ((r.timestamp-origin)&0xffffffff)<=60000000]
            errors=(c.c_double*3)(); statuses=(c.c_int*2)(); arr=(Record*len(records))(*records)
            assert lib.test_fixed_preprocess_error(arr,len(records),10,1,errors,statuses)==0
            assert list(statuses)==[0,0]
            baseline=None
            for name,mode in [('float',1),('q15_fft',2),('fixed_preprocess',3),('fixed_both',4)]:
                started=time.perf_counter(); status,result=runner.run_case(records,single=mode)
                elapsed=time.perf_counter()-started
                if baseline is None:baseline=result
                k=result.selected_bin if result.selected_bin>=0 else result.best_bin
                b=baseline.selected_bin if baseline.selected_bin>=0 else baseline.best_bin
                row=dict(session=path.parent.name,variant=name,status=status,records=len(records),
                    host_seconds=elapsed,selected_bin=result.selected_bin,best_bin=result.best_bin,
                    candidate_psd_bpm=result.bins[k].psd_bpm if k>=0 else None,
                    candidate_acf_bpm=result.bins[k].acf_bpm if k>=0 else None,
                    candidate_psd_delta=result.bins[k].psd_bpm-baseline.bins[b].psd_bpm if k>=0 and b>=0 else None,
                    candidate_acf_delta=result.bins[k].acf_bpm-baseline.bins[b].acf_bpm if k>=0 and b>=0 else None,
                    acceptance_changed=(result.selected_bin>=0)!=(baseline.selected_bin>=0),
                    best_bin_changed=result.best_bin!=baseline.best_bin,
                    selected_bin_changed=result.selected_bin!=baseline.selected_bin,
                    per_bin_acceptance_changes=sum(result.bins[j].accepted!=baseline.bins[j].accepted for j in range(result.n_bins)),
                    max_score_delta=max(abs(result.bins[j].score-baseline.bins[j].score) for j in range(result.n_bins)),
                    preprocess_max_abs_error=errors[0],preprocess_rms_error=errors[1],
                    compared_samples=int(errors[2]))
                rows.append(row)
            print(path.parent.name,flush=True)
    finally:exp.AmplitudeExperimentTests.tearDownClass()
    Path(sys.argv[1]).write_text(json.dumps(rows,indent=2)+'\n')

if __name__=='__main__':main()
