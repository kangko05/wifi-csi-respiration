"""Same-session host benchmark. Does not change production build settings.
Run: python/.venv/Scripts/python.exe benchmarks/speed.py
One warm-up and three measured runs per configuration; builds/imports excluded.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"python/scripts"))
from run_c_amplitude import decode, digest
sys.path.insert(0,str(ROOT/"python/vendor/wifi-csi-proto/src"))
from csi_pipeline import load_session
from csi_pipeline.phase_cir import adapt_session, PhaseCirConfig, decide_features
from csi_pipeline._vendor.wifi_csi_backup import layout
from csi_pipeline._vendor.wifi_csi_backup.preprocess import gain, phase, resample

def measure(function, repeat):
    function()  # warm-up
    samples=[]
    result=None
    for _ in range(repeat):
        start=perf_counter(); result=function(); samples.append(perf_counter()-start)
    return {"seconds":samples,"median_s":statistics.median(samples)},result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session",default="20260916T060200_034260_9dd9fd80")
    parser.add_argument("--repeat",type=int,default=3)
    args=parser.parse_args()
    if args.repeat<1 or Path(args.session).name!=args.session: parser.error("invalid arguments")
    folder=ROOT/"data"/args.session
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out=ROOT/"outputs/speed"/stamp; out.mkdir(parents=True)
    files=[folder/"serial.bin",folder/"session.json"]
    before={str(p):digest(p) for p in files}
    report={"session":args.session,"repeat":args.repeat,"warmups":1,"platform":platform.platform(),
        "processor":platform.processor(),"source_sha256":before,"results":{},
        "notes":["Host timings, not ESP32-C5 timings; build and Python imports excluded.",
            "C process includes pipe/text parsing/preprocessing/startup; algorithm timer excludes them.",
            "Python phase uses preloaded legacy arrays, only phase (no CIR), no plotting.",
            "Default native numerical-library thread settings are preserved."]}
    report["thread_environment"]={k:os.environ.get(k) for k in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS")}
    report["compiler"]=subprocess.check_output(["gcc","--version"],text=True).splitlines()[0]
    timing,raw=measure(lambda:(folder/"serial.bin").read_bytes(),args.repeat)
    report["results"]["read_file"]=timing
    timing,(payload,metadata)=measure(lambda:decode(raw),args.repeat)
    report["results"]["decode_and_text_encode"]=timing
    report["input"]=metadata
    executables={}
    for optimization in ("O0","O3"):
        exe=out/f"timed_{optimization}.exe"
        subprocess.run(["gcc","-std=c11",f"-{optimization}","-DNDEBUG","-I",str(ROOT/"include"),
            str(ROOT/"benchmarks/timed_main.c"),*[str(ROOT/"src"/f) for f in
            ("amplitude.c","phase.c","filters.c","preprocess.c","csi_utils.c")],"-o",str(exe),"-lm"],check=True)
        executables[optimization]=exe
    outputs={}
    for method in ("amplitude","phase"):
        for optimization,exe in executables.items():
            algorithms=[]
            def run():
                proc=subprocess.run([str(exe),"--stdin"]+(["--phase"] if method=="phase" else []),
                    input=payload,text=True,capture_output=True,check=True)
                algorithms.append(float(proc.stderr.strip()))
                return json.loads(proc.stdout)
            timing,result=measure(run,args.repeat)
            timing["algorithm_seconds"]=algorithms[1:]
            timing["algorithm_median_s"]=statistics.median(algorithms[1:])
            timing["transport_and_input_median_s"]=statistics.median(
                a-b for a,b in zip(timing["seconds"],algorithms[1:]))
            key=f"c_{method}_{optimization}"
            report["results"][key]=timing; outputs[key]=result
            print(key,json.dumps(timing),flush=True)
    source=load_session(ROOT/"python/outputs/legacy_run_20260917/derived"/args.session)
    derived=json.loads((source.path/"session.json").read_text(encoding="utf-8"))
    assert derived["derived_from"]["source_raw_sha256"]==before[str(folder/"serial.bin")]
    stages=[]
    def python_phase():
        values={}; start=perf_counter()
        block,_,_=adapt_session(source,PhaseCirConfig()); values["adapt"]=perf_counter()-start
        start=perf_counter(); corrected=phase.correct(gain.correct(block,"rms_norm"),"los_wls")
        values["gain_and_phase"]=perf_counter()-start
        start=perf_counter(); uniform,fs=resample.to_uniform(corrected)
        dynamic=uniform.H-uniform.H.mean(axis=0); values["resample_and_center"]=perf_counter()-start
        start=perf_counter(); result=decide_features("phase",dynamic,dynamic,fs,5,layout.HT40.take,"original_buffer_column")
        values["spectrum_and_waveform"]=perf_counter()-start; stages.append(values)
        return result
    timing,result=measure(python_phase,args.repeat)
    timing["stage_medians_s"]={k:statistics.median(s[k] for s in stages[1:]) for k in stages[0]}
    report["results"]["python_phase"]=timing
    print("python_phase",json.dumps(timing),flush=True)
    phase_outputs=[outputs[f"c_phase_{opt}"] for opt in executables]
    report["phase_results_equal"]=all(abs(r["diagnostic_bpm"]-result.diagnostic_bpm)<1e-8 and
        abs(r["sharpness"]-result.sharpness)<1e-7 and bool(r["accepted"])==result.accepted and
        abs(r["peak_count_bpm"]-result.peak_count_bpm)<1e-8 for r in phase_outputs)
    a,b=(outputs[f"c_amplitude_{opt}"] for opt in executables)
    report["amplitude_results_equal"]=a==b
    report["sources_unchanged"]=all(digest(p)==before[str(p)] for p in files)
    report["code_sha256"]={str(p.relative_to(ROOT)):digest(p) for p in
        [*ROOT.glob("src/*.c"),*ROOT.glob("include/csi_resp/*.h"),*ROOT.glob("benchmarks/*") ] if p.is_file()}
    (out/"timings.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(out,flush=True)
    assert report["sources_unchanged"] and report["phase_results_equal"] and report["amplitude_results_equal"]

if __name__=="__main__": main()
