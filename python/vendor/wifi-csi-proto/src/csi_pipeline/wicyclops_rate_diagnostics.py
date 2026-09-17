"""Post-estimation evaluation and plots for the Section 7 adaptation."""

from __future__ import annotations

import base64
from html import escape
from pathlib import Path

import numpy as np

from .periodicity_diagnostics import evaluate_reference


def evaluate_rows(rows, references):
    for row in rows:
        row.update(evaluate_reference(row, row["original_duration_s"], references.get(row["session_id"])))
        row["empty_room_rate_output"] = row.pop("empty_room_periodic_candidate")


def rate_totals(rows, windows):
    totals = {}
    for method, band, rule in sorted({(r["method"], r["band"], r["peak_rule"]) for r in rows}):
        records = [r for r in rows if (r["method"], r["band"], r["peak_rule"]) == (method, band, rule)]
        groups = {}
        for group in ("counted", "uncertain", "movement", "empty", "unavailable"):
            subset = [r for r in records if r["reference_group"] == group]
            errors = [r["absolute_error_to_reference_range_bpm"] for r in subset
                      if r["absolute_error_to_reference_range_bpm"] is not None]
            ids = {r["session_id"] for r in subset}
            parts = [r for r in windows if r["session_id"] in ids
                     and (r["method"], r["band"], r["peak_rule"]) == (method, band, rule)]
            groups[group] = dict(
                n_sessions=len(subset), n_with_output=sum(r["candidate_bpm"] is not None for r in subset),
                mae_among_outputs_bpm=float(np.mean(errors)) if errors else None,
                median_absolute_error_bpm=float(np.median(errors)) if errors else None,
                n_error_below_0_5_bpm=sum(e < 0.5 for e in errors),
                n_windows=len(parts), n_windows_with_output=sum(r["candidate_bpm"] is not None for r in parts),
            )
        totals[f"{method}/{band}/{rule}"] = groups
    return totals


def plot_rates(session_id, full_results, windows):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 2, figsize=(13, 10), layout="constrained")
    fig.suptitle(f"{session_id} | Section 7 adaptation | offline rate outputs, not presence decisions")
    colors = {"reference_band": "#2563eb", "wide_band": "#db6c18"}
    for column, method in enumerate(("none", "smoothed")):
        prepared, estimates = full_results[method]
        axes[0, column].set_title(method)
        if prepared.reason is None:
            for band, result in estimates.items():
                color = colors[band]
                axes[0, column].plot(prepared.bin_indices, result.score, color=color, label=band, lw=1)
                axes[0, column].scatter(prepared.bin_indices[result.selected_mask],
                                        result.score[result.selected_mask], color=color, s=12)
                lag = np.arange(len(result.combined_acf)) / prepared.sample_rate_hz
                axes[1, column].plot(lag, result.combined_acf, color=color, lw=1, label=band)
                for rule, marker in (("literal", "x"), ("band_limited", "o")):
                    record = result.summary(rule)
                    if record["peak_lag_s"] is not None:
                        axes[1, column].scatter(record["peak_lag_s"], record["peak_height"],
                                                color=color, marker=marker, s=45)
        else:
            axes[0, column].text(0.1, 0.5, prepared.reason, transform=axes[0, column].transAxes)
        for band, color in colors.items():
            parts = [r for r in windows if r["method"] == method and r["band"] == band
                     and r["peak_rule"] == "band_limited"]
            axes[2, column].plot([(r["start_s"] + r["end_s"]) / 2 for r in parts],
                                [r["candidate_bpm"] if r["candidate_bpm"] is not None else np.nan for r in parts],
                                ".-", color=color, lw=0.8, markersize=3, label=band)
        axes[0, column].set(xlabel="Original bin (dots: selected)", ylabel="Eq. 11 score")
        axes[1, column].set(xlabel="Lag (s); x: literal peak, o: band-limited", ylabel="Eq. 13 weighted ACF",
                            xlim=(0, 20))
        axes[2, column].set(xlabel="20 s window center (s); no window ground truth", ylabel="Band-limited bpm",
                            ylim=(0, 50))
        axes[0, column].legend(fontsize=8)
        for row in range(3):
            axes[row, column].grid(alpha=0.2)
    return fig


def write_rate_html(output: Path, rows, totals):
    def number(value):
        return "—" if value is None else f"{value:.2f}"

    parts = ["""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>WiCyclops Section 7 comparison</title><style>
body{font:15px/1.6 system-ui,sans-serif;max-width:1250px;margin:32px auto;padding:0 20px;color:#172033}
table{border-collapse:collapse;width:100%;margin:18px 0}th,td{border:1px solid #d5dbe5;padding:7px;text-align:left}
th{background:#edf2f8}img{max-width:100%}summary{cursor:pointer;font-weight:600}details{margin:18px 0}
.note{background:#fff4dd;padding:15px;border-radius:6px}</style>
<h1>WiCyclops §7 호흡수 추정 비교</h1>
<p>진폭 분산·BNR 점수 → 후보 열 선택 → 점수 가중 ACF → 첫 피크 지연의 호흡수 환산.</p>
<p class="note">숫자 출력은 호흡 존재 판정이 아니다. 이 비교는 전체 기록에서 전처리한 오프라인
개발 자료 분석이다. 수동 카운트 동기화가 확인되지 않았고, 창별 정답은 없다.</p>
<p><b>reference_band</b>: BNR 인용 문헌의 10–30회/분. <b>wide_band</b>: 프로젝트의 3–48회/분.
<b>literal</b>: 지연 0 이후 첫 국소 최대. <b>band_limited</b>: 해당 대역의 지연 안에서 첫 국소 최대라는 로컬 보완 규칙.
모든 설정을 실측 실행 전에 고정했다. 두 피크 규칙 모두 높이 문턱값은 없다.</p>
<p>기존 PSD 추정기의 집중도·ACF·주기 수 조건을 적용하지 않는다. 기본 20초 창·1초 간격이며,
기존 30초 창의 채택 수와 직접 비교할 수 없다. 원본의 약 50/100Hz에서 짧은 보간만 수행하고,
추가 저역통과·10Hz 변환·선형 추세 제거는 하지 않았다.</p>
<h2>전체 기록 사후 평가</h2><table><tr><th>처리 / BNR / 피크 규칙</th><th>카운트 출력</th>
<th>출력분 MAE (회/분)</th><th>오차 &lt;0.5 건수</th><th>빈 방 출력</th></tr>"""]
    for name, groups in totals.items():
        counted, empty = groups["counted"], groups["empty"]
        parts.append(f"<tr><td>{escape(name)}</td><td>{counted['n_with_output']}/{counted['n_sessions']}</td>"
                     f"<td>{number(counted['mae_among_outputs_bpm'])}</td>"
                     f"<td>{counted['n_error_below_0_5_bpm']}/{counted['n_sessions']}</td>"
                     f"<td>{empty['n_with_output']}/{empty['n_sessions']}</td></tr>")
    parts.append("</table><p>MAE는 출력이 있는 일반 카운트 기록에 한정한 평균 절대 오차다. "
                 "오차 &lt;0.5 건수는 논문의 동기화 센서 기반 검출률 재현이 아니다. "
                 "불확실·움직임 구분과 창별 출력 수는 evaluation.json에 별도로 기록했다.</p>"
                 "<p><a href='summary.csv'>전체 수치</a> · <a href='windows.csv'>창 수치</a> · "
                 "<a href='evaluation.json'>구분별 집계</a> · <a href='manifest.json'>설정·해시</a></p>")
    for session_id in sorted({r["session_id"] for r in rows}):
        records = [r for r in rows if r["session_id"] == session_id]
        parts.append(f"<details><summary>{escape(session_id)} · {escape(records[0]['reference_group'])}</summary>"
                     "<table><tr><th>처리 / 대역 / 규칙</th><th>출력</th><th>수동 기준</th><th>절대 오차</th><th>선택 열</th></tr>")
        for r in records:
            parts.append(f"<tr><td>{escape(r['method'] + '/' + r['band'] + '/' + r['peak_rule'])}</td>"
                         f"<td>{number(r['candidate_bpm'])}</td>"
                         f"<td>{number(r['reference_bpm_low'])}–{number(r['reference_bpm_high'])}</td>"
                         f"<td>{number(r['absolute_error_to_reference_range_bpm'])}</td><td>{r['n_selected_bins']}</td></tr>")
        picture = base64.b64encode((output / f"{session_id}.png").read_bytes()).decode("ascii")
        parts.append(f"</table><img alt='{escape(session_id)} score, ACF and window outputs' src='data:image/png;base64,{picture}'></details>")
    parts.append("</html>")
    (output / "index.html").write_text("\n".join(parts), encoding="utf-8")
