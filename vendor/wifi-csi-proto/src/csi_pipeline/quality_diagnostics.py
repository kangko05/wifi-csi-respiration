"""Serializable tables and plots for stage 3, separate from processing."""

from __future__ import annotations

import base64
from dataclasses import asdict
import html
from pathlib import Path

import numpy as np

from .quality import QualityResult, WindowQuality


def window_record(result: QualityResult, window: WindowQuality, index: int | str) -> dict:
    return {
        "session_id": result.source.source.session_id,
        "gain_method": result.source.config.method,
        "window_index": index,
        "start_s": window.start_s,
        "end_s": window.end_s,
        "start_index": window.start_index,
        "stop_index": window.stop_index,
        "includes_end": window.includes_end,
        "data_usable": window.data_usable,
        "blocking_reasons": "|".join(window.blocking_reasons),
        "warnings": "|".join(window.warnings),
        **window.metrics,
    }


def session_record(result: QualityResult) -> dict:
    row = window_record(result, result.full_session, "full_session")
    row["nominal_interval_s"] = result.nominal_interval_s
    row["n_windows"] = len(result.windows)
    row["n_data_usable_windows"] = sum(w.data_usable for w in result.windows)
    row["sliding_tail_s"] = (
        max(0.0, result.full_session.end_s - result.windows[-1].end_s)
        if result.windows else result.full_session.end_s - result.full_session.start_s
    )
    row["hardware_validity_flag_recorded"] = (
        "rx_channel_estimate_info_vld" in result.source.source.metadata
    )
    return row


def plot_quality(result: QualityResult):
    import matplotlib.pyplot as plt

    session = result.source.source
    times = session.time_s
    fig, axes = plt.subplots(5, 1, figsize=(12, 12), sharex=True, layout="constrained")
    fig.suptitle(f"{session.session_id} | stage 3 data quality", fontsize=14)
    axes[0].semilogy(times[1:], np.diff(times) * 1000, color="#334155", lw=0.6)
    axes[0].axhline(result.nominal_interval_s * 1000, ls="--", color="#94a3b8")
    axes[0].set_ylabel("Packet interval\n(ms, log scale)")
    axes[0].set_title(
        f"Gap limit {result.config.max_gap_s * 1000:g} ms | "
        f"median interval {result.nominal_interval_s * 1000:g} ms",
        fontsize=10, loc="left",
    )
    for name, color in (("agc_gain", "#059669"), ("fft_gain", "#64748b")):
        axes[1].step(times, session.metadata[name], where="post", lw=0.6, label=name, color=color)
    axes[1].set_ylabel("Recorded\ngain states")
    axes[1].legend(loc="upper right", ncol=2)
    lines = [("raw", result.raw_rms, result.raw_step_db, "#2563eb")]
    if result.source.config.method != "none":
        lines.append((result.source.config.method, result.corrected_rms, result.corrected_step_db, "#ea580c"))
    for name, rms, steps, color in lines:
        # Dots cannot draw an apparent interpolated line across missing time.
        axes[2].plot(times, rms, ".", ms=1.1, alpha=0.6, label=name, color=color, rasterized=True)
        axes[3].plot(times, np.abs(steps), ".", ms=1.1, alpha=0.6, label=name, color=color, rasterized=True)
    axes[2].set_ylabel("Packet RMS\n(arbitrary units)")
    axes[3].set_ylabel("Adjacent common-bin\nRMS step (absolute dB)")
    axes[2].legend(loc="upper right", ncol=2)
    axes[3].legend(loc="upper right", ncol=2)
    axes[4].set_ylabel("Eligible bins\n(% of session-valid bins)")
    axes[4].set_ylim(-5, 105)
    if result.windows:
        starts = [w.start_s for w in result.windows]
        fractions = [w.metrics["eligible_bin_fraction"] * 100 for w in result.windows]
        colors = ["#059669" if w.data_usable else "#dc2626" for w in result.windows]
        axes[4].scatter(starts, fractions, c=colors, s=16)
        width = result.config.window_s
        label = "Full capture" if width is None else f"{width:g} s windows; points at window START"
        axes[4].set_title(label + " | green: data usable; red: insufficient data", fontsize=10, loc="left")
    else:
        axes[4].text(0.5, 0.5, "No complete requested windows; see full-session assessment",
                     transform=axes[4].transAxes, ha="center")
    axes[-1].set_xlabel("Original time from first aligned packet (s)")
    for ax in axes:
        ax.grid(alpha=0.15)
        ax.set_xlim(times[0], times[-1])
    return fig


def save_arrays(path: Path, result: QualityResult) -> None:
    """Store masks with original row/column mappings; never serialize objects."""
    session = result.source.source
    width = len(session.bin_indices)
    windows = result.windows

    def stack(name, dtype):
        return np.stack([getattr(w, name) for w in windows]) if windows else np.empty((0, width), dtype=dtype)

    np.savez_compressed(
        path, time_s=session.time_s, bin_indices=session.bin_indices,
        metadata_row_indices=session.metadata_row_indices,
        valid_sample_mask=session.valid_sample_mask,
        window_start_s=np.array([w.start_s for w in windows]),
        window_end_s=np.array([w.end_s for w in windows]),
        start_index=np.array([w.start_index for w in windows], dtype=np.int64),
        stop_index=np.array([w.stop_index for w in windows], dtype=np.int64),
        includes_end=np.array([w.includes_end for w in windows], dtype=bool),
        data_usable=np.array([w.data_usable for w in windows], dtype=bool),
        eligible_bin_mask=stack("eligible_bin_mask", bool),
        bin_valid_fraction=stack("bin_valid_fraction", float),
        bin_max_valid_gap_s=stack("bin_max_valid_gap_s", float),
        full_eligible_bin_mask=result.full_session.eligible_bin_mask,
        full_bin_valid_fraction=result.full_session.bin_valid_fraction,
        full_bin_max_valid_gap_s=result.full_session.bin_max_valid_gap_s,
        raw_rms=result.raw_rms, corrected_rms=result.corrected_rms,
        raw_step_db=result.raw_step_db, corrected_step_db=result.corrected_step_db,
        gain_transition=result.gain_transition, coefficient_change=result.coefficient_change,
    )


REASONS = {
    "too_few_packets": "패킷 부족",
    "too_few_valid_packets": "유효 패킷 부족",
    "low_valid_packet_fraction": "유효 패킷 비율 부족",
    "long_observation_gap": "긴 관측 공백",
    "no_eligible_bins": "사용 가능한 열 없음",
    "gain_transition": "gain 전환",
    "recorded_coefficient_change": "저장 계수 변화",
    "invalid_packets": "무효 패킷",
    "masked_observations": "열별 무효 관측",
    "irregular_packet_spacing": "긴 패킷 간격 관측",
}


def _number(value, scale=1.0):
    return "—" if value is None else f"{value * scale:.3f}"


def _reasons(codes):
    return ", ".join(REASONS.get(code, code) for code in codes) or "없음"


def write_html(output: Path, results: list[QualityResult]) -> None:
    """Self-contained report with native HTML disclosure panels, no scripts."""
    summary_rows, sections = [], []
    for index, result in enumerate(results):
        row = session_record(result)
        name = html.escape(row["session_id"])
        summary_rows.append(
            f'<tr><td><a href="#session-{index}">{name}</a></td>'
            f'<td>{row["n_data_usable_windows"]}/{row["n_windows"]}</td>'
            f'<td>{_number(row["max_observation_gap_s"], 1000)}</td>'
            f'<td>{row["n_gain_transitions"]}</td>'
            f'<td>{_number(row["raw_abs_step_db_max"])}</td>'
            f'<td>{_number(row["corrected_abs_step_db_max"])}</td></tr>'
        )
        window_rows = []
        for window in result.windows:
            metrics = window.metrics
            state = "데이터 사용 가능" if window.data_usable else "데이터 부족"
            reasons = html.escape(_reasons(window.blocking_reasons))
            warnings = html.escape(_reasons(window.warnings))
            window_rows.append(
                f'<tr><td>{window.start_s:.3f}–{window.end_s:.3f}</td>'
                f'<td>{state}</td><td>{metrics["n_packets"]}</td>'
                f'<td>{metrics["n_eligible_bins"]}/{metrics["n_session_valid_bins"]}</td>'
                f'<td>{_number(metrics["max_observation_gap_s"], 1000)}</td>'
                f'<td>{reasons}</td><td>{warnings}</td></tr>'
            )
        if not window_rows:
            window_rows.append('<tr><td colspan="7">요청 길이를 만족하는 완전한 창이 없습니다.</td></tr>')
        png = base64.b64encode((output / f"{row['session_id']}.png").read_bytes()).decode("ascii")
        full_state = "데이터 사용 가능" if result.full_session.data_usable else "데이터 부족"
        sections.append(
            f'<details id="session-{index}" {"open" if index == 0 else ""}><summary>{name}</summary>'
            f'<p>전체 기록 {row["end_s"] - row["start_s"]:.3f}초 · {full_state} · '
            f'유효 관측 {_number(row["valid_observation_fraction"], 100)}% · '
            f'전체 기록 사용 가능 열 {row["n_eligible_bins"]}/{row["n_session_valid_bins"]}</p>'
            f'<p>전체 기록 사유: {html.escape(_reasons(result.full_session.blocking_reasons))}. '
            f'마지막 분석 창 뒤의 시간: {row["sliding_tail_s"]:.3f}초. '
            '전체 기록 통계와 NPZ는 이 부분도 포함합니다.</p>'
            f'<img src="data:image/png;base64,{png}" alt="{name} 패킷 간격, gain 상태, RMS 변동, 창별 품질">'
            '<div class="scroll"><table><thead><tr><th>창 (초)</th><th>상태</th><th>패킷</th>'
            '<th>사용 가능 열</th><th>최대 공백 ms</th><th>사용 불가 사유</th><th>참고 정보</th>'
            '</tr></thead><tbody>' + "".join(window_rows) + '</tbody></table></div></details>'
        )
    config = html.escape(str(asdict(results[0].config)))
    method = html.escape(results[0].source.config.method)
    page = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wi-Fi CSI · 구간화와 품질 평가</title>
<style>
body{margin:0;background:#f4f6fa;color:#172033;font:16px/1.65 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:24px}section,details{background:white;padding:20px;margin:18px 0;border-radius:10px}
h1,h2{line-height:1.3}summary{font-size:18px;font-weight:600;cursor:pointer;overflow-wrap:anywhere}
img{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;vertical-align:top}
.scroll{overflow:auto}code{overflow-wrap:anywhere}a{color:#1d4ed8}.note{color:#475569}
</style></head><body><main><h1>Wi-Fi CSI · 모듈 ③ 구간화·품질 평가</h1>
<p>실제 시간과 마스크를 유지한 상태에서 후속 분석에 사용할 데이터가 있는지 확인합니다.</p>
<section><h2>판정의 범위</h2>
<p><strong>데이터 사용 가능은 호흡 검출 성공을 뜻하지 않습니다.</strong> 빈 방과 움직임 기록도 데이터 조건을 만족할 수 있습니다.
gain 전환은 참고 정보로 남기며, 이 사실만으로 창을 제외하지 않습니다. 움직임 판정 임계값과 호흡 신뢰도는 아직 없습니다.</p>
<p>기본값: 30초 창, 1초 간격, 최대 관측 공백 0.25초, 유효 관측 비율 90%, 최소 2개 관측.
이는 임시 데이터 처리 설정입니다. 최소 2개는 계산 가능성의 하한이며 호흡 추정에 충분한 관측량이 아닙니다.</p>
<p>창은 [시작, 끝)이며 완전한 창만 만듭니다. 전체 기록 평가는 마지막 패킷까지 포함합니다.
짧은 꼬리를 늘리거나 보간하지 않습니다. 약 5회/분 기록은 30초에 약 2.5주기뿐이므로 전체 기록 분석도 유지합니다.</p>
<p>유효 비율의 분모는 <strong>수집된 패킷 × 세션에서 유효한 열</strong>입니다.
117개 원시 열 중 전체 기록에서 무효인 열은 분모에서 제외합니다. 패킷 손실률이나 시간 커버리지와 같지 않습니다.
최대 공백은 창 양끝과 유효 관측 사이의 여백도 포함합니다. 열마다 같은 공백 검사를 적용합니다.</p>
<p>패킷 RMS는 각 시각의 유효 열로 계산합니다. 인접 변화(dB)는 두 패킷에서 공통으로 유효한 열만 비교하며,
긴 공백·무효 패킷을 건너 연결하지 않습니다. 전환·변화 사건은 뒤쪽 패킷 시각에 귀속하므로 창 시작 직전 패킷과의 차이를 포함할 수 있습니다.</p>
<p>열별 상대 진폭 폭은 (95백분위−5백분위)/중앙값입니다. CSV에 열별 값의 중앙값·95백분위도 남깁니다.
RMS·진폭 폭은 강한 필터링 전의 진단값이며 움직임 원인이나 호흡 여부를 확정하지 않습니다.</p>
<p class="note">실제 타임스탬프를 사용하고 필터링·재표본화·호흡 정답을 사용하지 않았습니다.
명목 간격은 전체 세션의 중앙값을 사용하므로 오프라인 평가입니다. 긴 간격 개수는 패킷 손실 개수로 해석하지 않습니다.
기존 수집 파일의 하드웨어 유효 플래그·gain 보정 준비 상태는 미기록입니다.</p>
<p>gain 경로: <strong>__METHOD__</strong><br>실제 실행 설정: <code>__CONFIG__</code></p></section>
<section><h2>전체 세션</h2><div class="scroll"><table><thead><tr>
<th>세션</th><th>데이터 사용 가능 창/전체 창</th><th>전체 최대 공백 ms</th><th>gain 전환</th>
<th>원본 최대 변화 dB</th><th>선택 경로 최대 변화 dB</th></tr></thead><tbody>__ROWS__</tbody></table></div></section>
__SECTIONS__
<p class="note">summary.csv: 전체 기록 · windows.csv: 창별 지표와 사유 · 세션별 NPZ: 시간·원본 행/열 번호·마스크·진단 배열.
manifest.json에 설정과 입력·코드 해시를 기록합니다. 그림은 HTML에 내장되어 있습니다.</p>
</main></body></html>"""
    page = page.replace("__METHOD__", method).replace("__CONFIG__", config)
    page = page.replace("__ROWS__", "".join(summary_rows)).replace("__SECTIONS__", "".join(sections))
    (output / "index.html").write_text(page, encoding="utf-8")
