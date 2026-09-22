"""Checklist 2.1: preregistered full-record FIR comparison and matched crop."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
from importlib.metadata import version
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from scipy import signal

from csi_pipeline import QualityConfig, assess_quality, correct_gain, load_session
from csi_pipeline.exploration_diagnostics import (
    add_evaluation_groups, collect_diagnostics, common_totals, distribution,
    flatten_grouped_totals, grouped_totals, high_frequency_summary, join_references,
)
from csi_pipeline.periodicity import PeriodicityConfig, estimate_periodicity
from csi_pipeline.periodicity_diagnostics import load_references, save_full_arrays
from csi_pipeline.temporal_filter import TemporalFilterConfig, design_temporal_filter
from assess_quality import FILENAMES, fingerprints, write_csv


VARIANTS = {
    "none": None,
    "matched_none": TemporalFilterConfig(kind="trim"),
    "bandpass": TemporalFilterConfig(kind="bandpass", low_hz=.1, high_hz=.8),
    "lowpass": TemporalFilterConfig(kind="lowpass", high_hz=1.),
}


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def verify_baseline(previous, rows, hashes):
    manifest = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "complete" or manifest["input_sha256"] != hashes:
        raise ValueError("previous report incomplete or input hashes differ")
    with (previous / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        saved = {r["session_id"]: r for r in csv.DictReader(handle) if r["method"] == "none"}
    raw = [r for r in rows if r["method"] == "none"]
    if set(saved) != {r["session_id"] for r in raw}:
        raise ValueError("baseline session sets differ")
    # All existing numeric signal diagnostics; evaluation joins happen later.
    fields = [k for k in collect_diagnostic_fields() if k not in ("session_id", "method")]
    for row in raw:
        for key in fields:
            old = float(saved[row["session_id"]][key]) if saved[row["session_id"]][key] else None
            new = row[key]
            if (old is None) != (new is None) or (old is not None and not np.isclose(old, new, atol=1e-10, rtol=1e-10)):
                raise ValueError(f"baseline mismatch: {row['session_id']}/{key}")
    return dict(matched=True, n_records=len(raw), fields=fields)


def collect_diagnostic_fields():
    return ("session_id", "method", "original_duration_s", "native_rate_hz", "best_psd_bpm", "best_bin",
            "best_acf_bpm", "best_acf_peak", "best_concentration", "best_score", "candidate_bpm",
            "resolution_bpm", "n_analyzed_bins", "n_nonflat_bins", "high_frequency_power_ratio_median",
            "analysis_duration_s", "filter_edge_trim_s", "retained_fraction_of_input")


def html_table(rows, columns):
    def fmt(value):
        return "—" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
    return ("<table><tr>" + "".join(f"<th>{escape(label)}</th>" for _, label in columns) + "</tr>" +
            "".join("<tr>" + "".join(f"<td>{escape(fmt(row[key]))}</td>" for key, _ in columns) + "</tr>" for row in rows) + "</table>")


def write_report(output, rows, totals, groups, responses):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout="constrained")
    for method in VARIANTS:
        subset = [r for r in rows if r["method"] == method and r["reference_group"] == "counted"]
        errors = sorted(r["best_error_bpm"] for r in subset if r["best_error_bpm"] is not None)
        axes[0].step(errors, np.arange(1, len(errors) + 1) / len(subset), where="post", label=method)
    axes[0].set(xlabel="Diagnostic peak absolute error (bpm)", ylabel="Fraction of counted sessions", ylim=(0, 1.05))
    for method in ("bandpass", "lowpass"):
        entry = next(r for r in responses if r["method"] == method)
        f, h = signal.freqz(entry["taps"], worN=4096, fs=entry["sample_rate_hz"])
        axes[1].plot(f, 20 * np.log10(np.maximum(abs(h), 1e-8)), label=method)
    axes[1].set(xlim=(0, 1.6), ylim=(-100, 5), xlabel="Frequency (Hz)", ylabel="FIR amplitude response (dB)")
    for ax in axes:
        ax.grid(alpha=.2)
        ax.legend()
    fig.suptitle(f"{output.name} | Development diagnostics and finite FIR response")
    fig.savefig(output / "comparison.png", dpi=130)
    plt.close(fig)
    parts = ["""<!doctype html><html lang="ko"><meta charset="utf-8"><title>2.1 대역 제한 비교</title>
<style>body{font:15px/1.6 system-ui;max-width:1250px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{padding:7px;border:1px solid #ccd}th{background:#eef2f6}img{width:100%}details{margin:20px 0}</style>
<h1>2.1 대역 제한 · 같은 시간 구간의 원본 대조군 포함</h1>
<p><b>25개 개발 자료 기준, 독립 검증 아님.</b> 수동 기준은 모든 추정이 끝난 뒤 전체 기록 평가에만 사용했다.
최고 점수 진단 피크와 기존 규칙이 채택한 출력은 별도다. 임계값·최고 열 선택·집중도 분모·PSD/ACF 계산은 유지했다.</p>
<h2>실행 전 고정한 처리</h2><p>원본 진폭 → 기존 마스크·최대 0.1초 공백 검사/보간 → 기존 Kaiser FIR 안티앨리어싱·약 10Hz 변환·양끝 약 1초 제외
→ 추가 FIR 또는 같은 길이 자르기 → 선형 추세·평균 제거 → 기존 PSD·ACF. 전체 기록만 평가하며 게인·WiCyclops는 추가하지 않았다.</p>
<p>bandpass: 0.1–0.8Hz, lowpass: 1Hz. Kaiser β=8 대칭 FIR, 추가 반폭 10초를 출력 표본 수로 올림한다.
약 10Hz에서 차수는 약 200이며 실제 차수·계수·응답은 filter_designs.json에 저장했다.
한 번의 중심 합성곱으로 완전한 필터 지지가 있는 값만 남긴다. 인과 구현의 지연은 약 10초이며 시간 좌표 보상 후 위상 지연은 0이다.
미래 표본이 필요한 오프라인 처리다. 컷오프는 유한 FIR 전이대역의 명목값이며 벽처럼 제거하는 경계가 아니다.</p>
<p>none은 기존 전체 길이, matched_none은 추가 양끝 10초를 자른 원본이다. matched_none/bandpass/lowpass의 시간·열·해상도는 실행 중 일치 검사했다.
긴 공백은 메우지 않는다. 30초 창에는 이 설정의 필터 지지와 최소 20초 분석 길이를 함께 확보할 수 없어 적용 시 보류한다.
짧아진 중앙 구간에도 전체 기록 수동 평균을 비교하므로 구간별 호흡 변화는 확인할 수 없다.</p>
<h2>counted 진단 피크 공통 평가</h2><p>한 칸은 제로 패딩 전 실제 해상도다. 짧아진 경로는 허용 오차 한 칸이 넓어지므로 고정 ±1.1회/분도 같이 본다.
×2/÷2는 기본 주파수 일치를 제외한 한 해상도 이내 진단이며 확정 고조파 판정이 아니다.</p>"""]
    overview = []
    for method in VARIANTS:
        group = totals[method]["counted"]
        overview.append(dict(method=method, median=group["best_error_bpm"]["median"],
                             one=group["n_within_one_resolution_bin"], two=group["n_within_two_resolution_bins"],
                             fixed=group["n_within_1_1_bpm"], fixed_two=group["n_within_2_2_bpm"],
                             double=group["n_double_like"], half=group["n_half_like"], accepted=group["n_accepted"]))
    parts.append(html_table(overview, [("method", "경로"), ("median", "오차 중앙값"), ("one", "≤1칸 /19"),
        ("two", "≤2칸 /19"), ("fixed", "≤1.1 /19"), ("fixed_two", "≤2.2 /19"), ("double", "×2 부근"),
        ("half", "÷2 부근"), ("accepted", "채택 /19")]))
    parts.append('<img src="comparison.png" alt="진단 피크 오차 누적분포와 FIR 주파수 응답">')
    parts.append("<h2>채택 출력 평가</h2><p>진단 최고 열과 채택 열은 다를 수 있다. 아래 오차는 실제 채택 출력만 평가한 값이다.</p>")
    selected = []
    for method in VARIANTS:
        values = [r["accepted_error_bpm"] for r in rows if r["method"] == method and r["reference_group"] == "counted"]
        dist = distribution(values)
        selected.append(dict(method=method, n=dist["n"], median=dist["median"], max=dist["max"],
                             fixed=sum(v is not None and v <= 1.1 for v in values)))
    parts.append(html_table(selected, [("method", "경로"), ("n", "채택 /19"), ("median", "오차 중앙값"), ("max", "최대 오차"), ("fixed", "≤1.1 /19")]))
    parts.append("<h2>유효 시간·해상도</h2>")
    parts.append(html_table(rows, [("session_id", "기록"), ("method", "경로"), ("analysis_start_s", "시작 초"),
        ("analysis_end_s", "끝 초"), ("analysis_duration_s", "길이 초"), ("resolution_bpm", "해상도 bpm"), ("filter_edge_trim_s", "편측 총 경계 초")]))
    for cohort in ("counted", "uncertain", "movement", "empty"):
        parts.append(f"<h2>{cohort} 증거값</h2>")
        if cohort == "empty":
            parts.append("<p>빈 방 3개는 별도 재실 판단이 잘못 켜졌을 때의 강제 입력 참고다. 숫자 출력만으로 시스템 오탐률이나 호흡 모듈 실패를 판정하지 않는다. 실제 재실 모듈은 연결하지 않았다.</p>")
        parts.append(html_table([r for r in rows if r["reference_group"] == cohort], [
            ("session_id", "기록"), ("method", "경로"), ("best_psd_bpm", "진단 bpm"), ("best_concentration", "집중도"),
            ("best_acf_peak", "ACF"), ("high_frequency_power_ratio_median", ">0.8Hz 비율"), ("candidate_bpm", "채택 bpm")]))
    parts.append("<h2>거리·50/100Hz·교차 그룹</h2><p>파일명 거리와 실제 중앙 수집 간격의 기술 통계다. 수집 속도의 인과 효과로 해석하지 않는다. 모든 참조 그룹은 CSV/JSON에 보존했다.</p>")
    parts.append(html_table([r for r in groups if r["reference_group"] == "counted"], [
        ("dimension", "차원"), ("subgroup", "그룹"), ("method", "경로"), ("n_sessions", "N"),
        ("median_error_bpm", "오차 중앙값"), ("n_within_1_1_bpm", "≤1.1"), ("n_accepted", "채택")]))
    parts.append("<p>집중도·ACF 상승이나 고주파 비율 하락 자체는 호흡 정확도 증거가 아니다. 후속 2.2에서 지표 정의를 별도 비교한다.</p>")
    for filename in ("summary.csv", "evaluation.json", "group_evaluation.csv", "paired_comparison.csv", "accepted_evaluation.json", "high_frequency_summary.json", "filter_designs.json", "manifest.json"):
        parts.append(f'<a href="{filename}">{filename}</a> · ')
    parts.append("</html>")
    (output / "index.html").write_text("\n".join(parts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", default="band001")
    parser.add_argument("--output", type=Path, default=Path("outputs/exploration/band001"))
    parser.add_argument("--previous-report", type=Path, default=Path("outputs/exploration/prep003"))
    parser.add_argument("--save-arrays", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_relative_to(repository / "outputs") or output == repository / "outputs":
        parser.error("output must be an experiment subdirectory of outputs")
    if output.exists() and any(output.iterdir()):
        parser.error("output exists; use a new experiment ID and directory")
    if output.name != args.experiment_id:
        parser.error("output directory name must match experiment ID")
    paths = sorted(p for p in (repository / "data").iterdir() if p.is_dir())
    reference = repository / "references/manual_breath_counts.csv"
    inputs = [p / name for p in paths for name in FILENAMES]
    code = [Path(__file__), repository / "scripts/assess_quality.py", repository / "pyproject.toml",
            repository / "references/band_limit_experiment.md",
            *sorted((repository / "src/csi_pipeline").glob("*.py")), *sorted((repository / "tests").glob("test_*.py"))]
    previous_files = sorted(p for p in args.previous_report.iterdir() if p.is_file())
    inputs_hash, code_hash = fingerprints(inputs), fingerprints(code)
    ref_hash, previous_hash = fingerprints([reference]), fingerprints(previous_files)
    config, quality_config = PeriodicityConfig(), QualityConfig(window_s=None)
    manifest = dict(
        artifact_kind="wifi-csi-band-limit-v1", experiment_id=args.experiment_id, status="in_progress",
        started_at_utc=datetime.now(timezone.utc).isoformat(),
        variants={k: asdict(v) if v else None for k, v in VARIANTS.items()},
        periodicity_config=asdict(config), quality_config=asdict(quality_config), save_arrays=args.save_arrays,
        processing_order=["raw amplitude", "bounded interpolation", "existing FIR anti-alias and downsample",
                          "existing edge trim", "centered valid FIR or matched trim", "linear detrend and mean removal", "existing PSD and ACF"],
        fir_design="odd Kaiser beta=8 FIR; order=2*ceil(10*actual_output_rate); single centered valid convolution",
        phase="zero retained delay; causal delay=half support; offline future samples required",
        comparison="none has original support; matched_none/bandpass/lowpass have identical central times and resolution",
        labels_used_in_estimation=False, thresholds_tuned=False, high_frequency_threshold_hz=.8,
        error_reference="whole-record manual midpoint / original duration, including for trimmed paths; no synchronized central labels",
        scope="25 development sessions; full records only; not independent validation; no occupancy gate",
        permutation="prep003 retained; preparation and shuffle tests not repeated",
        input_sha256=inputs_hash, code_sha256=code_hash, reference_sha256=ref_hash, previous_report_sha256=previous_hash,
        versions={name: version(name) for name in ("numpy", "scipy", "matplotlib")},
    )
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "manifest.json", manifest)  # Before any signal estimation.
    rows, responses = [], []
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=1):
        for path in paths:
            quality = assess_quality(correct_gain(load_session(path)), quality_config)
            matched = None
            for method, filtering in VARIANTS.items():
                result = estimate_periodicity(quality, config, temporal_filter=filtering)
                full = result.full_session
                if method == "matched_none":
                    matched = full
                if method in ("bandpass", "lowpass"):
                    for a, b in ((full.time_s, matched.time_s), (full.bin_indices, matched.bin_indices),
                                 (full.interpolated_mask, matched.interpolated_mask)):
                        np.testing.assert_array_equal(a, b)
                    if full.resolution_hz != matched.resolution_hz:
                        raise RuntimeError("matched resolutions differ")
                row = collect_diagnostics(result, method)
                row.update(analysis_start_s=float(full.time_s[0]) if full.time_s.size else None,
                           analysis_end_s=float(full.time_s[-1]) if full.time_s.size else None,
                           n_analysis_samples=len(full.time_s), reasons="|".join(full.best.reasons if full.best else full.reasons))
                rows.append(row)
                if filtering is not None and full.sample_rate_hz:
                    taps, half = design_temporal_filter(filtering, full.sample_rate_hz)
                    frequencies = [0, .01, .05, .1, .25, .5, .8, 1., 1.5, 2.]
                    _, response = signal.freqz(taps, worN=frequencies, fs=full.sample_rate_hz)
                    responses.append(dict(session_id=path.name, method=method, sample_rate_hz=full.sample_rate_hz,
                                          order=2 * half, causal_delay_s=half / full.sample_rate_hz,
                                          retained_phase_delay_s=0., taps=taps.tolist(),
                                          response_frequencies_hz=frequencies, response_magnitude=abs(response).tolist()))
                if args.save_arrays:
                    save_full_arrays(output / f"{path.name}__{method}.npz", result)
            print(f"{path.name}: four paths; matched time/columns/masks verified", flush=True)
    previous_check = verify_baseline(args.previous_report, rows, inputs_hash)
    join_references(rows, load_references(reference))
    add_evaluation_groups(rows)
    for row in rows:
        row["accepted_error_bpm"] = (abs(row["candidate_bpm"] - row["reference_bpm"])
                                     if row["candidate_bpm"] is not None and row["reference_bpm"] is not None else None)
    totals = common_totals(rows)
    grouped = grouped_totals(rows)
    group_rows = flatten_grouped_totals(grouped)
    paired = []
    controls = {r["session_id"]: r for r in rows if r["method"] == "matched_none"}
    for row in rows:
        if row["method"] not in ("bandpass", "lowpass"):
            continue
        control = controls[row["session_id"]]
        entry = {key: row[key] for key in ("session_id", "method", "reference_group", "distance_group", "sampling_rate_group")}
        for key in ("best_error_bpm", "best_concentration", "best_acf_peak", "high_frequency_power_ratio_median"):
            entry[f"delta_{key}_vs_matched_none"] = row[key] - control[key] if row[key] is not None and control[key] is not None else None
        paired.append(entry)
    accepted = {}
    for method in VARIANTS:
        accepted[method] = {}
        for cohort in ("counted", "uncertain", "movement", "empty"):
            subset = [r for r in rows if r["method"] == method and r["reference_group"] == cohort]
            errors = [r["accepted_error_bpm"] for r in subset if r["accepted_error_bpm"] is not None]
            accepted[method][cohort] = dict(n_sessions=len(subset), n_accepted=sum(r["candidate_bpm"] is not None for r in subset),
                                           error_bpm=distribution(errors), mean_error_bpm=float(np.mean(errors)) if errors else None,
                                           n_within_1_1_bpm=sum(e <= 1.1 for e in errors))
    for name, values in (("summary.csv", rows), ("group_evaluation.csv", group_rows), ("paired_comparison.csv", paired)):
        write_csv(output / name, values, list(values[0]))
    for name, value in (("evaluation.json", totals), ("group_evaluation.json", grouped), ("accepted_evaluation.json", accepted),
                        ("high_frequency_summary.json", {method: high_frequency_summary([dict(r, method="none") for r in rows if r["method"] == method]) for method in VARIANTS}),
                        ("filter_designs.json", responses)):
        save_json(output / name, value)
    import matplotlib
    matplotlib.use("Agg")
    write_report(output, rows, totals, group_rows, responses)
    for files, before in ((inputs, inputs_hash), (code, code_hash), ([reference], ref_hash), (previous_files, previous_hash)):
        if fingerprints(files) != before:
            raise RuntimeError("input, code, reference or previous report changed during experiment")
    manifest.update(status="complete", completed_at_utc=datetime.now(timezone.utc).isoformat(),
                    n_sessions=len(paths), n_rows=len(rows), n_npz_files=len(list(output.glob("*.npz"))),
                    previous_report_check=previous_check, matched_support_check=True)
    save_json(output / "manifest.json", manifest)
    for method in VARIANTS:
        print(method, json.dumps(totals[method]["counted"]))
    print(f"Report: {output / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
