"""Compare amplitude, independent phase, independent CIR and optional legacy fusion."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
from importlib.metadata import version
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from csi_pipeline import QualityConfig, assess_quality, correct_gain, load_session
from csi_pipeline.exploration_diagnostics import add_evaluation_groups
from csi_pipeline.periodicity import PeriodicityConfig, estimate_periodicity
from csi_pipeline.periodicity_diagnostics import load_references
from csi_pipeline.phase_cir import PhaseCirConfig, PhaseCirUnavailable, run_phase_cir
from csi_pipeline.spectral_evidence import EvidenceConfig, column_evidence
from assess_quality import FILENAMES, fingerprints, write_csv
from evaluate_band_limits import html_table, save_json


def blank_row(session, method):
    return dict(session_id=session.session_id, method=method, status="unavailable",
        original_duration_s=float(np.ptp(session.time_s)), native_rate_hz=float(1/np.median(np.diff(session.time_s))),
        diagnostic_bpm=None, candidate_bpm=None, spectral_bpm=None, peak_count_bpm=None,
        estimate_method=None, accepted=False, sharpness=None, agreement_bpm=None,
        concentration=None, acf_peak=None, amplitude_local_snr=None, backup_band_snr=None,
        analysis_start_s=None, analysis_end_s=None, analysis_duration_s=None, resolution_bpm=None,
        search_grid_step_bpm=None, n_used_raw_packets=None, n_used_raw_columns=None,
        n_selected_features=None, selected_feature_indices="", feature_kind=None,
        layout_profile=None, filter_edge_policy=None, reasons="")


def evaluate_rows(rows, references):
    """Join manual counts only after all method decisions have been computed."""
    for row in rows:
        ref = references.get(row["session_id"])
        target = ((ref["low"]+ref["high"])/2*60/row["original_duration_s"]
                  if ref and ref["group"] != "empty" else None)
        row.update(reference_group=ref["group"] if ref else "unavailable", reference_bpm=target,
                   reference_note=ref["note"] if ref else "", diagnostic_error_bpm=None,
                   accepted_error_bpm=None, within_1_1_bpm=None, within_2_2_bpm=None,
                   within_one_resolution_bin=None)
        if target is not None and row["diagnostic_bpm"] is not None:
            error = abs(row["diagnostic_bpm"]-target)
            row.update(diagnostic_error_bpm=error, within_1_1_bpm=error<=1.1, within_2_2_bpm=error<=2.2,
                       within_one_resolution_bin=error<=row["resolution_bpm"] if row["resolution_bpm"] else None)
        if target is not None and row["candidate_bpm"] is not None:
            row["accepted_error_bpm"] = abs(row["candidate_bpm"]-target)
    add_evaluation_groups(rows)


def totals(rows, methods):
    result=[]
    for method in methods:
        subset=[r for r in rows if r["method"]==method]
        errors=[r["diagnostic_error_bpm"] for r in subset if r["diagnostic_error_bpm"] is not None]
        accepted=[r["accepted_error_bpm"] for r in subset if r["accepted_error_bpm"] is not None]
        result.append(dict(method=method,n=len(subset),available=sum(r["diagnostic_bpm"] is not None for r in subset),
            median_error=float(np.median(errors)) if errors else None,
            within_1_1=sum(r["within_1_1_bpm"] is True for r in subset),
            within_2_2=sum(r["within_2_2_bpm"] is True for r in subset),
            within_resolution=sum(r["within_one_resolution_bin"] is True for r in subset),
            accepted=sum(r["accepted"] for r in subset),accepted_hits=sum(e<=1.1 for e in accepted),
            accepted_errors=sum(e>1.1 for e in accepted)))
    return result


TOTAL_COLUMNS=[("method","방식"),("n","N"),("available","진단 출력"),("median_error","진단 오차 중앙값"),
               ("within_1_1","±1.1"),("within_2_2","±2.2"),("within_resolution","실제1칸"),
               ("accepted","채택"),("accepted_hits","채택 중±1.1"),("accepted_errors","채택 중>1.1")]


def plot_session(path, session_id, plots):
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(max(1,len(plots)),2,figsize=(13,3.1*max(1,len(plots))),layout="constrained",squeeze=False)
    for (method,p),pair in zip(plots.items(),axes):
        time,wave,f,power,kind=p
        if len(time) and len(wave):
            pair[0].plot(time,wave,lw=.7)
        else:
            pair[0].text(.5,.5,"Unavailable / flat",transform=pair[0].transAxes,ha="center")
        if len(f):
            pair[1].plot(f*60,power,lw=1)
        pair[0].set(title=method+" | own waveform",xlabel="Original time (s)",ylabel="Processed value")
        pair[1].set(title=method+" | "+kind,xlabel="Frequency (bpm scale)",ylabel=kind,xlim=(3,60))
        for ax in pair:
            ax.grid(alpha=.2)
    fig.suptitle(session_id+" | separate decisions; shared phase preprocessing for phase/CIR",fontsize=12)
    fig.savefig(path,dpi=120)
    plt.close(fig)


def write_report(output, rows, methods):
    far=[r for r in rows if r["reference_group"]=="counted" and r["distance_group"]=="far_90_100cm"]
    counted=[r for r in rows if r["reference_group"]=="counted"]
    parts=['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>진폭·위상·CIR 독립 판정 비교</title><style>body{font:15px/1.6 system-ui;max-width:1500px;margin:auto;padding:24px;background:#f6f8fb;color:#172033}section,details{background:white;margin:18px 0;padding:18px;border-radius:8px}.scroll{overflow:auto}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #ccd;padding:6px}th{background:#edf2f7}img{width:100%}summary{cursor:pointer;font-weight:bold}a{color:#1d4ed8}</style>',
        '<h1>진폭·위상·CIR · 독립 판정 비교</h1><section><h2>멂90–100cm · counted</h2>',
        '<div class="scroll">'+html_table(totals(far,methods),TOTAL_COLUMNS)+'</div>',
        '<p>수동 기준이 제공된 기록만 평가한다. 참고 진단값과 채택 출력은 분리했다. 독립된 호흡 검증이 아니며 빈 방을0bpm 정답으로 사용하지 않는다.</p></section>',
        '<section><h2>전체 counted</h2><div class="scroll">'+html_table(totals(counted,methods),TOTAL_COLUMNS)+'</div></section>',
        '<section><h2>각 경로가 독립적으로 결정하는 내용</h2><p><b>amplitude:</b> 기존 none 전체 기록 PSD/ACF. '
        '<b>phase:</b> 보정 CSI의 도플러+같은 CSI에서 만든 파형의 피크 개수. '
        '<b>cir:</b> 앞8tap 자체의 도플러+같은 tap 파형의 피크 개수. CIR 판정에 phase의 추정 주파수를 넣지 않는다. '
        '<b>legacy_combined(옵션):</b> 기존 백업처럼 CSI 도플러를 CIR 파형 피크 개수와 결합한 대조군.</p>',
        '<p>위상/CIR은 백업 기본 RMS 정규화·LoS WLS 위상 보정을 공유하므로 통계적으로 독립된 증거가 아니다. '
        '각 경로에서 자기 스펙트럼 주파수 주변으로 파형을 다시 대역 제한해 피크를 세므로, 스펙트럼과 피크 개수의 일치도 완전히 독립된 검증은 아니다.</p>',
        '<p>위상/CIR 채택 규칙은 백업 sharpness&gt;1.55 및 자체 주파수 차이&lt;3bpm이다. '
        '<b>CIR에 이 기준을 적용한 독립 판정은 새 연결이며 CIR 전용 검증이 안 된 임시 기준이다.</b> '
        '대역 경계 fallback을 포함한 진단값과 순수 스펙트럼/피크 카운트 값은 따로 저장했다. 진폭 집중도0.35·국소 SNR3은 변경하지 않았다.</p>',
        '<p>백업의117열 HT40 주파수 순서를 명시적 가정으로 사용한다. null 위치 검사만으로 물리 순서를 입증하지 않았다. '
        '백업의114열 압축 IFFT를 보존했으므로 CIR tap을 정확한 거리나 직접 경로 분리로 해석하지 않는다. '
        '백업 주파수 탐색 간격0.005Hz(0.3bpm)는 실제 분해능이 아니다. 표의 실제1칸은Fs/N이고 고정±1.1/±2.2와 구분한다.</p>',
        '<p>무효 패킷은 원본 시간/행 번호와 함께 제외하고0.1초 초과 공백은 판정을 보류한다. '
        '진폭은 FIR 경계를 제외하고 위상/CIR은 백업 IIR의 padding을 유지한 전체 기록이므로 시간 지지가 다를 수 있다. '
        '각 실제 시작/끝을 아래와 CSV에 표시했다. 백업 원본과 캡처는 수정하지 않았다.</p></section>']
    for group in ("near_30_50cm","middle_60_75cm"):
        subset=[r for r in counted if r["distance_group"]==group]
        if subset:
            parts.append('<section><h2>'+group+'</h2><div class="scroll">'+html_table(totals(subset,methods),TOTAL_COLUMNS)+'</div></section>')
    for cohort in ("empty","uncertain","movement","unavailable"):
        subset=[r for r in rows if r["reference_group"]==cohort]
        if subset:
            parts.append('<section><h2>'+cohort+'</h2><div class="scroll">'+html_table(totals(subset,methods),TOTAL_COLUMNS)+
                         '</div><p>기준이 없는 기록의 적중0은 실패 횟수가 아니라 평가할 수 없다는 뜻이다.</p></section>')
    ids=sorted({r["session_id"] for r in rows},key=lambda name:(not any(r["session_id"]==name for r in far),name))
    columns=[("method","방식"),("status","상태"),("reference_bpm","수동 평균"),("diagnostic_bpm","진단 bpm"),
        ("candidate_bpm","채택 bpm"),("spectral_bpm","스펙트럼 bpm"),("peak_count_bpm","피크 카운트 bpm"),
        ("sharpness","백업 sharpness"),("agreement_bpm","자체 차이 bpm"),("analysis_start_s","시작 초"),
        ("analysis_end_s","끝 초"),("resolution_bpm","실제1칸 bpm"),("reasons","보류 이유")]
    for i,name in enumerate(ids):
        parts.append(f'<details {"open" if i==0 else ""}><summary>{escape(name)}</summary><div class="scroll">'+
            html_table([r for r in rows if r["session_id"]==name],columns)+
            f'</div><img loading="lazy" src="{escape(name)}.png" alt="{escape(name)} 경로별 파형과 스펙트럼">'
            f'<a href="{escape(name)}.npz">원본 좌표·마스크·파형·스펙트럼 NPZ</a></details>')
    parts.append('<p><a href="summary.csv">모든 결과 CSV</a> · <a href="evaluation.json">평가 요약</a> · '
                 '<a href="manifest.json">설정·출처·해시</a></p></html>')
    (output/'index.html').write_text('\n'.join(parts),encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path',nargs='?',type=Path,default=Path('data'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--methods',nargs='+',choices=('amplitude','phase','cir'),default=['amplitude','phase','cir'])
    parser.add_argument('--include-legacy',action='store_true')
    parser.add_argument('--reference-csv',type=Path,help='Evaluation only; never an estimator input')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1]
    source=args.path.resolve()
    if not source.is_dir():
        parser.error('input must be a session or parent directory')
    output=(args.output or root/'outputs/method_comparison'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')).resolve()
    if not output.is_relative_to(root/'outputs') or output==root/'outputs' or output.is_relative_to(source):
        parser.error('output must be a new directory under outputs, outside input')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('nonempty output exists; use a new directory')
    if args.reference_csv and args.reference_csv.resolve().is_relative_to(output):
        parser.error('reference must be outside output')
    paths=[source] if any((source/n).exists() for n in FILENAMES) else sorted(p for p in source.iterdir() if p.is_dir())
    if not paths:
        parser.error('no sessions')
    methods=list(dict.fromkeys(args.methods))+(['legacy_combined'] if args.include_legacy else [])
    inputs=[p/n for p in paths for n in FILENAMES]
    references=[args.reference_csv.resolve()] if args.reference_csv else []
    code=[Path(__file__),root/'scripts/assess_quality.py',root/'scripts/evaluate_band_limits.py',
          root/'main.py',root/'references/phase_cir_backup.json',root/'references/phase_cir_integration.md',
          *sorted((root/'src/csi_pipeline').rglob('*.py'))]
    hashes=dict(input_sha256=fingerprints(inputs),reference_sha256=fingerprints(references),code_sha256=fingerprints(code))
    config,amp_config=PhaseCirConfig(),PeriodicityConfig()
    manifest=dict(status='in_progress',artifact_kind='independent-phase-cir-comparison-v1',
        started_at_utc=datetime.now(timezone.utc).isoformat(),methods=methods,phase_cir_config=asdict(config),
        amplitude_config=asdict(amp_config),quality_config=asdict(QualityConfig(window_s=None)),
        labels_used_in_estimation=False,thresholds_tuned=False,
        phase_cir_gate={'sharpness_strictly_above':1.55,'agreement_strictly_below_bpm':3.,'cir_gate_validated':False},
        versions={name:version(name) for name in ('numpy','scipy','matplotlib')},**hashes)
    output.mkdir(parents=True,exist_ok=True)
    save_json(output/'manifest.json',manifest)
    import matplotlib
    matplotlib.use('Agg')
    from threadpoolctl import threadpool_limits
    rows=[]
    with threadpool_limits(limits=1):
        for path in paths:
            session=load_session(path)
            arrays=dict(source_time_s=session.time_s,source_bin_indices=session.bin_indices,
                source_metadata_row_indices=session.metadata_row_indices,source_valid_sample_mask=session.valid_sample_mask)
            plots={}
            if 'amplitude' in methods:
                result=estimate_periodicity(assess_quality(correct_gain(session),QualityConfig(window_s=None)),amp_config)
                full=result.full_session
                best=full.best
                row=blank_row(session,'amplitude')
                evidence=column_evidence(full,amp_config,EvidenceConfig())
                row.update(status='accepted' if full.selected else ('withheld' if best else 'unavailable'),
                    diagnostic_bpm=best.psd_hz*60 if best else None,spectral_bpm=best.psd_hz*60 if best else None,
                    candidate_bpm=full.selected.psd_hz*60 if full.selected else None,accepted=full.selected is not None,
                    estimate_method='amplitude_psd_acf',concentration=best.concentration if best else None,
                    acf_peak=best.acf_peak if best else None,
                    amplitude_local_snr=float(evidence.local_snr[full.best_index]) if best and np.isfinite(evidence.local_snr[full.best_index]) else None,
                    analysis_start_s=float(full.time_s[0]) if len(full.time_s) else None,
                    analysis_end_s=float(full.time_s[-1]) if len(full.time_s) else None,
                    analysis_duration_s=float(np.ptp(full.time_s)) if len(full.time_s) else None,
                    resolution_bpm=full.resolution_hz*60 if full.resolution_hz else None,
                    search_grid_step_bpm=float(np.diff(full.frequencies_hz)[0]*60) if len(full.frequencies_hz)>1 else None,
                    n_used_raw_packets=int(session.valid_packet_mask.sum()),n_used_raw_columns=len(full.bin_indices),
                    n_selected_features=1 if best else 0,selected_feature_indices=str(best.bin_index) if best else '',
                    feature_kind='original_buffer_column',filter_edge_policy='FIR edges excluded',
                    reasons='|'.join(best.reasons if best else full.reasons))
                rows.append(row)
                wave=full.processed_amplitude[:,full.best_index] if best else np.empty(0)
                power=full.psd[:,full.best_index] if best else np.zeros(len(full.frequencies_hz))
                plots['amplitude']=(full.time_s,wave,full.frequencies_hz,power/max(float(power.max(initial=0)),1e-30),'normalized diagnostic PSD')
                arrays.update(amplitude_time_s=full.time_s,amplitude_waveform=wave,amplitude_frequencies_hz=full.frequencies_hz,
                    amplitude_power=power,amplitude_processed_bin_indices=full.bin_indices,
                    amplitude_input_grid_time_s=full.input_grid_time_s,amplitude_interpolated_mask=full.interpolated_mask)
            requested=[m for m in methods if m!='amplitude']
            if requested:
                try:
                    result=run_phase_cir(session,config,include_legacy=args.include_legacy)
                except PhaseCirUnavailable as exc:
                    for method in requested:
                        row=blank_row(session,method)
                        row['reasons']=str(exc)
                        rows.append(row)
                        plots[method]=(np.empty(0),np.empty(0),np.empty(0),np.empty(0),'unavailable')
                else:
                    support=result.support
                    arrays.update(phase_cir_time_s=support.time_s,phase_cir_original_row_indices=support.original_row_indices,
                        phase_cir_original_bin_indices=support.original_bin_indices,
                        phase_cir_assumed_frequency_offsets_hz=support.assumed_frequency_offsets_hz,
                        phase_cir_interpolated_time_mask=support.interpolated_time_mask)
                    for method in requested:
                        decision=getattr(result,method)
                        row=blank_row(session,method)
                        row.update(status='accepted' if decision.accepted else ('withheld' if decision.diagnostic_bpm else 'unavailable'),
                            diagnostic_bpm=decision.diagnostic_bpm,candidate_bpm=decision.accepted_bpm,
                            spectral_bpm=decision.spectral_bpm,peak_count_bpm=decision.peak_count_bpm,
                            estimate_method=decision.estimate_method,accepted=decision.accepted,sharpness=decision.sharpness,
                            agreement_bpm=decision.agreement_bpm,backup_band_snr=decision.band_snr,
                            analysis_start_s=float(support.time_s[0]),analysis_end_s=float(support.time_s[-1]),
                            analysis_duration_s=float(np.ptp(support.time_s)),resolution_bpm=support.resolution_hz*60,
                            search_grid_step_bpm=.3,n_used_raw_packets=len(support.original_row_indices),
                            n_used_raw_columns=len(support.original_bin_indices),n_selected_features=len(decision.selected_feature_indices),
                            selected_feature_indices='|'.join(map(str,decision.selected_feature_indices)),feature_kind=decision.feature_kind,
                            layout_profile=support.layout_profile,filter_edge_policy='legacy IIR padding retained; untrimmed',
                            reasons='|'.join(decision.reasons))
                        rows.append(row)
                        plots[method]=(support.time_s,decision.waveform,decision.frequencies_hz,decision.whitened_power,'whitened spectral score')
                        arrays.update({method+'_'+key:value for key,value in dict(waveform=decision.waveform,
                            frequencies_hz=decision.frequencies_hz,power=decision.spectral_power,whitened_power=decision.whitened_power,
                            selected_feature_indices=decision.selected_feature_indices).items()})
            np.savez_compressed(output/f'{path.name}.npz',**arrays)
            plot_session(output/f'{path.name}.png',path.name,plots)
            print(path.name+': '+', '.join(f"{r['method']}={r['diagnostic_bpm']} ({r['status']})" for r in rows if r['session_id']==path.name),flush=True)
    evaluate_rows(rows,load_references(references[0]) if references else {})
    write_csv(output/'summary.csv',rows,list(rows[0]))
    far=[r for r in rows if r['reference_group']=='counted' and r['distance_group']=='far_90_100cm']
    evaluation=dict(far_counted=totals(far,methods),all_counted=totals([r for r in rows if r['reference_group']=='counted'],methods),
        cohorts={group:totals([r for r in rows if r['reference_group']==group],methods) for group in ('empty','uncertain','movement','unavailable')})
    save_json(output/'evaluation.json',evaluation)
    write_report(output,rows,methods)
    for key,files in (('input_sha256',inputs),('reference_sha256',references),('code_sha256',code)):
        if fingerprints(files)!=hashes[key]:
            raise RuntimeError(key+' changed during run')
    manifest.update(status='complete',completed_at_utc=datetime.now(timezone.utc).isoformat(),n_sessions=len(paths),n_rows=len(rows))
    save_json(output/'manifest.json',manifest)
    print('Far:',evaluation['far_counted'])
    print('Report:',output/'index.html')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
