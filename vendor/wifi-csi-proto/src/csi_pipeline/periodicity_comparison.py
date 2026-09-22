"""Availability-aware reporting for a fixed estimator across preprocessing stages."""

from __future__ import annotations

import base64
from collections import Counter
import html
from pathlib import Path

import numpy as np

from .periodicity import summarize_periodicity
from .periodicity_diagnostics import evaluation_totals

METHODS = ("none", "pca", "density", "smoothed")


def comparison_totals(rows, windows):
    totals = {}
    for method in METHODS:
        full = [r for r in rows if r["method"] == method]
        local = [r for r in windows if r["method"] == method]
        empty_ids = {r["session_id"] for r in full if r["reference_group"] == "empty"}
        empty_windows = [r for r in local if r["session_id"] in empty_ids]
        retention = [r["preprocessing_retained_fraction"] for r in full]
        totals[method] = dict(
            references=evaluation_totals(full),
            n_sessions=len(full),
            n_analyzable_sessions=sum(r["n_processed_bins"] > 0 for r in full),
            n_candidate_sessions=sum(r["candidate_bpm"] is not None for r in full),
            n_windows=len(local),
            n_analyzable_windows=sum(r["n_processed_bins"] > 0 for r in local),
            n_candidate_windows=sum(r["candidate_bpm"] is not None for r in local),
            n_empty_windows=len(empty_windows),
            n_empty_candidate_windows=sum(r["candidate_bpm"] is not None for r in empty_windows),
            mean_session_retained_fraction=float(np.mean(retention)) if retention else None,
            full_rejection_reasons=dict(Counter(
                reason for r in full if r["candidate_bpm"] is None
                for reason in r["reasons"].split("|") if reason
            )),
        )
    return totals


def plot_comparison(results):
    """Stage-specific selected/best bins; no references are used for selection."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(4, 2, figsize=(13, 11), layout="constrained")
    session = results["none"].amplitude.source.session_id
    fig.suptitle(f"{session} | same periodicity rules across preprocessing stages", fontsize=13)
    for row, method in enumerate(METHODS):
        result, (left, right) = results[method], axes[row]
        full = result.full_session
        summary = summarize_periodicity(full)
        index = full.selected_index if full.selected_index is not None else full.best_index
        if index is not None:
            c = full.candidates[index]
            left.plot(full.frequencies_hz * 60, full.psd[:, index], color="#2563eb", lw=1)
            left.axvline(c.psd_hz * 60, color="#ea580c", ls="--", lw=1)
            state = "accepted" if full.selected else "withheld"
            left.set_title(f"{method}: {state}, bin {c.bin_index}, {c.psd_hz * 60:.2f} bpm", fontsize=10)
        else:
            left.text(.5, .5, "\n".join(full.reasons), ha="center", va="center", transform=left.transAxes)
            left.set_title(f"{method}: no full-record candidate", fontsize=10)
        left.set_xlim(0, result.config.max_hz * 60 + 6)
        left.set_xlabel("Full-record PSD, search-band view (bpm)")
        left.set_ylabel("Power density")
        records = [summarize_periodicity(w) for w in result.windows]
        centers = [(r["start_s"] + r["end_s"]) / 2 for r in records]
        right.scatter(centers, [r["best_psd_bpm"] if r["best_psd_bpm"] is not None else np.nan for r in records],
                      s=13, color="#94a3b8", label="best attempt")
        right.scatter(centers, [r["candidate_bpm"] if r["candidate_bpm"] is not None else np.nan for r in records],
                      s=18, color="#2563eb", label="accepted")
        unavailable = [x for x, r in zip(centers, records) if r["n_processed_bins"] == 0]
        right.scatter(unavailable, [-2] * len(unavailable), marker="x", s=18, color="#dc2626", label="unavailable")
        right.set_ylim(-4, result.config.max_hz * 60 + 3)
        retained = summary["preprocessing_retained_fraction"]
        right.set_title(
            f"Full record: retained {retained:.1%}, bins after gap check {summary['n_bins_after_gap_check']}",
            fontsize=10,
        )
        right.set_xlabel("Window center (s); no window-level reference")
        right.set_ylabel("Candidate bpm")
        if row == 0:
            right.legend(loc="upper center", fontsize=7, ncol=3)
        left.grid(alpha=.15)
        right.grid(alpha=.15)
    return fig


def write_comparison_html(output: Path, rows, totals):
    def number(value):
        return "—" if value is None else f"{value:.2f}"

    overview, details = [], []
    for method in METHODS:
        t = totals[method]
        counted, empty = t["references"]["counted"], t["references"]["empty"]
        retention = t["mean_session_retained_fraction"]
        overview.append(
            f"<tr><th>{method}</th><td>{t['n_analyzable_sessions']}/{t['n_sessions']}</td>"
            f"<td>{counted['n_with_candidate']}/{counted['n_sessions']}</td>"
            f"<td>{number(counted['mae_among_returned_candidates_bpm'])}</td>"
            f"<td>{t['n_candidate_windows']}/{t['n_windows']}</td>"
            f"<td>{t['n_analyzable_windows']}/{t['n_windows']}</td>"
            f"<td>{empty['n_with_candidate']}/{empty['n_sessions']}</td>"
            f"<td>{t['n_empty_candidate_windows']}/{t['n_empty_windows']}</td>"
            f"<td>{retention:.1%}</td></tr>"
        )
    for session_id in dict.fromkeys(r["session_id"] for r in rows):
        records = [r for r in rows if r["session_id"] == session_id]
        first = records[0]
        ref = number(first["reference_bpm_low"])
        if first["reference_bpm_low"] != first["reference_bpm_high"]:
            ref += "–" + number(first["reference_bpm_high"])
        table = []
        for r in records:
            table.append(
                f"<tr><th>{r['method']}</th><td>{number(r['candidate_bpm'])}</td>"
                f"<td>{number(r['best_psd_bpm'])}</td>"
                f"<td>{number(r['absolute_error_to_reference_range_bpm'])}</td>"
                f"<td>{r['preprocessing_retained_fraction']:.1%}</td>"
                f"<td>{r['n_bins_after_retention']} → {r['n_bins_after_gap_check']}</td>"
                f"<td>{r['n_candidate_windows']}/{r['n_windows']}</td>"
                f"<td>{html.escape(r['reasons']) or '없음'}</td></tr>"
            )
        image = base64.b64encode((output / f"{session_id}.png").read_bytes()).decode("ascii")
        details.append(
            f"<details><summary>{html.escape(session_id)} · {first['reference_group']} · 전체 기준 {ref} bpm</summary>"
            f"<p>{html.escape(first['reference_note'])}</p><div class='scroll'><table><thead><tr>"
            "<th>처리</th><th>채택 bpm</th><th>최고 점수 시도 bpm</th><th>채택 오차 bpm</th>"
            "<th>관측 유지</th><th>유지율 → 공백 통과 열</th><th>채택 창</th><th>보류 사유</th>"
            f"</tr></thead><tbody>{''.join(table)}</tbody></table></div>"
            f"<img src='data:image/png;base64,{image}' alt='{html.escape(session_id)} 단계별 주기 후보 비교'></details>"
        )
    page = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WiCyclops 처리 후 주기 후보 비교</title>
<style>body{font:16px/1.65 system-ui,sans-serif;margin:0;color:#172033;background:#f4f6fa}
main{max-width:1250px;margin:auto;padding:24px}section,details{background:white;padding:20px;margin:20px 0;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}
summary{font-weight:bold;cursor:pointer;overflow-wrap:anywhere}.scroll{overflow:auto}img{width:100%;height:auto}</style></head>
<body><main><h1>WiCyclops 처리 후 주기 후보 비교</h1>
<p>같은 기록과 고정된 추정 규칙으로 원본 → PCA → DBSCAN → 평활화의 단계별 효과를 확인했습니다.</p>
<section><h2>주기 후보와 분석 가능 범위</h2>
<p>분석 가능은 처리 길이·관측 조건을 만족해 PSD·ACF를 계산한 상태입니다. 채택은 추가 근거 기준을 통과한 상태이며
호흡 존재 판정이 아닙니다. 후보가 없는 기록은 오차 계산에서 빠지므로 커버리지와 함께 읽어야 합니다.</p>
<div class="scroll"><table><thead><tr><th>처리</th><th>전체 분석 가능</th><th>일반 카운트 채택</th>
<th>채택분 MAE bpm</th><th>전체 창 채택</th><th>창 분석 가능</th><th>빈 방 기록 채택</th><th>빈 방 창 채택</th>
<th>세션 평균 관측 유지</th></tr></thead><tbody>__OVERVIEW__</tbody></table></div></section>
<section><h2>이번 비교의 조건</h2>
<ul><li>none은 ESP32 보정 계수와 WiCyclops를 적용하지 않은 기준선입니다. 모든 경로에 기존 보간·FIR·추세 제거·PSD·ACF를 적용했습니다.</li>
<li>PCA·density·smoothed는 기존 WiCyclops 구현을 전체 기록에 한 번 적용한 중간/최종 출력입니다.
DBSCAN은 각 열의 시간별 진폭 샘플에 적용하며, 서브캐리어끼리 묶지 않습니다. ESP32 게인 보정과 연쇄 적용하지 않았습니다.</li>
<li>유지율 90%, 최소 관측 수, 최대 공백 0.1초, 주파수 0.05–0.8Hz, ACF 0.3·집중도 0.35·최소 3주기 등은 기준선과 동일합니다.
PCA·DBSCAN 설정과 0.25초 평활화도 그대로 유지했습니다. 결과를 보고 조정하지 않았습니다.</li>
<li>제외한 관측은 원본 시각의 NaN과 마스크로 남깁니다. 유지율·공백 검사를 통과한 열의 짧은 공백만 기존 규칙으로 보간하고
그 위치를 기록합니다. 긴 공백을 메우거나 시간을 이어 붙이지 않습니다. 부호가 있는 PCA 재구성값에 절댓값을 다시 취하지 않습니다.</li>
<li>전체 기록을 사용한 PCA·군집화와 중심 이동평균이므로 창 밖 관측의 영향을 받는 오프라인 비교입니다.
30초 창 결과는 독립 실시간 평가가 아니며 서로 겹칩니다. PSD 그림은 대역 일부만 확대하지만 집중도의 분모는 전체 비DC 전력입니다.</li>
<li>수동 횟수는 모든 추정 후에 읽어 원본 전체 길이로 환산했습니다. 카운트 동기화는 미확인이고, 짧은 창에 정답을 복제하지 않습니다.
불확실 카운트·움직임은 별도 집계하고, 빈 방에는 0 bpm 목표를 두지 않습니다.
현재 실험은 개발용 비교이며 독립 정확도나 운영 오검출률의 검증이 아닙니다.</li></ul></section>
__DETAILS__
<p>summary.csv: 전체 기록·평가, windows.csv: 창별 결과, bin_candidates.csv: 열별 근거.
availability.csv: 원시 열별 유지율·공백·통과 여부. 경로별 NPZ: 실제 신호·마스크·좌표·PSD·ACF.
evaluation.json: 구분별 집계. manifest.json: 설정·해시·기준선 대조 결과.</p></main></body></html>"""
    page = page.replace("__OVERVIEW__", "".join(overview)).replace("__DETAILS__", "".join(details))
    (output / "index.html").write_text(page, encoding="utf-8")
