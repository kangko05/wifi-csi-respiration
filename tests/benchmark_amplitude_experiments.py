"""Reproducible recorded-input experiment; no ground truth is used in selection."""
import base64
import ctypes as c
import json
from pathlib import Path
import sys
import time
from test_amplitude_experiments import AmplitudeExperimentTests
from test_amplitude_raw import Record, ROOT

VARIANTS = [
    ('baseline',0,4,10,0,0), ('float_fft_fir',1,4,10,0,0),
    ('padding1',0,1,10,0,0), ('rate5',0,4,5,0,0), ('top16',0,4,10,16,0),
    ('stream',0,4,10,0,1), ('float_padding1',1,1,10,0,0),
    ('float_padding1_rate5',1,1,5,0,0), ('combined',1,1,5,16,0),
    ('combined_stream',1,1,5,16,1)]


def load_records(path):
    records=[]
    for line in path.read_bytes().splitlines():
        if not line.startswith(b'CSI_DATA,'): continue
        p=line.split(b',')
        if len(p)!=15: continue
        try:
            data=base64.b64decode(p[14],validate=True)
            r=Record(seq=int(p[1]),rssi=int(p[2]),noise_floor=int(p[3]),
                fft_gain=int(p[4]),agc_gain=int(p[5]),channel=int(p[6]),
                timestamp=int(p[7]),sig_len=int(p[8]),bb_format=int(p[9]),
                len=int(p[10]),first_word_invalid=int(p[11]),
                compensate_gain=float(p[12]),dropped=int(p[13]))
            if len(data)!=r.len or not 0<r.len<=512 or r.len%2: continue
            c.memmove(r.buf,data,len(data)); records.append(r)
        except (ValueError,OverflowError): continue
    return records


def main():
    AmplitudeExperimentTests.setUpClass()
    runner=AmplitudeExperimentTests()
    rows=[]
    try:
        for path in sorted((ROOT/'data').glob('*/serial.bin')):
            records=load_records(path)
            # First actual 60 seconds, matching the firmware's startup window.
            origin=records[0].timestamp
            records=[r for r in records if ((r.timestamp-origin)&0xffffffff)<=60000000]
            baseline=None
            combined=None
            for name,single,padding,rate,bins,stream in VARIANTS:
                t=time.perf_counter()
                status,result=runner.run_case(records,single,padding,rate,bins,stream)
                elapsed=time.perf_counter()-t
                if baseline is None: baseline=result
                if name=='combined': combined=result
                k=result.selected_bin if result.selected_bin>=0 else result.best_bin
                candidate=result.bins[k] if k>=0 else None
                basek=baseline.selected_bin if baseline.selected_bin>=0 else baseline.best_bin
                row=dict(session=path.parent.name,records=len(records),variant=name,
                    status=status,host_seconds=elapsed,samples=result.n_samples,
                    n_fft=result.n_fft,best_bin=result.best_bin,selected_bin=result.selected_bin,
                    psd_bpm=candidate.psd_bpm if candidate else None,
                    acf_bpm=candidate.acf_bpm if candidate else None,
                    reasons=candidate.reasons if candidate else None,
                    baseline_byte_equal=bytes(result)==bytes(baseline),
                    acceptance_changed=(result.selected_bin>=0)!=(baseline.selected_bin>=0),
                    baseline_candidate_retained=(basek<0 or not(result.bins[basek].reasons & 256)),
                    max_score_delta=max(abs(result.bins[j].score-baseline.bins[j].score) for j in range(result.n_bins)),
                    per_bin_acceptance_changes=sum(result.bins[j].accepted!=baseline.bins[j].accepted for j in range(result.n_bins)))
                if status: raise RuntimeError(f'{path} {name}: {status}')
                if name=='stream' and bytes(result)!=bytes(baseline):
                    raise AssertionError(f'stream mismatch: {path}')
                if name=='combined_stream' and bytes(result)!=bytes(combined):
                    raise AssertionError(f'combined stream mismatch: {path}')
                rows.append(row)
            print(path.parent.name,flush=True)
    finally:
        AmplitudeExperimentTests.tearDownClass()
    Path(sys.argv[1]).write_text(json.dumps(rows,indent=2)+'\n')

if __name__=='__main__': main()
