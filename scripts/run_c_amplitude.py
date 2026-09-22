"""Decode recorded base64 -> C main --stdin -> per-session JSON and summary.csv.

Run from repository root:
  .venv/bin/python scripts/run_c_amplitude.py
  ... --session 20260916T060200_034260_9dd9fd80
No signal processing is performed in Python. Each complete recording is one window.
Historical wrapper: the C sources/CMake project are not part of this layout.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from csi_collect.wire import parse_line

FIELDS = ("seq", "local_timestamp", "dropped", "compensate_gain", "rssi",
          "noise_floor", "fft_gain", "agc_gain", "channel", "rx_format",
          "first_word", "sig_len", "len")
LIMITS = {"seq": (0, 2**32-1), "local_timestamp": (0, 2**32-1),
          "dropped": (0, 2**32-1), "rssi": (-128,127), "noise_floor": (-128,127),
          "fft_gain": (-128,127), "agc_gain": (0,255), "channel": (0,255),
          "rx_format": (0,255), "first_word": (0,255), "sig_len": (0,65535), "len": (0,512)}
REASONS = ("excluded", "flat", "short", "band_edge", "few_cycles", "diffuse", "weak_acf", "disagreement")
REASON_LABELS = dict(zip(REASONS, (
    "유효 데이터 부족", "변동 없음", "분석 시간 부족", "탐색 대역 경계",
    "주기 수 부족", "PSD 집중도 부족", "자기상관 약함", "PSD·ACF 불일치")))


def print_result(report):
    session = report["session"]
    if report.get("error"):
        print(f"{session} | 오류: {report['error']}", flush=True)
        return
    if report.get("method") == "phase":
        label = "채택 후보" if report["accepted"] else "보류(진단값)"
        print(f"{session} | 위상 {label} | 스펙트럼 {report['spectral_bpm']:.2f} / "
              f"피크 계수 {report['peak_count_bpm']:.2f} bpm | "
              f"sharpness {report['sharpness']:.3f}", flush=True)
        return
    selected = report.get("selected_bin", -1)
    index = selected if selected >= 0 else report.get("best_bin", -1)
    if index < 0:
        print(f"{session} | 보류 | 분석 가능한 주기 후보 없음", flush=True)
        return
    b = report["bins"][index]
    psd = f"{b['psd_bpm']:.2f}" if b.get("psd_bpm") is not None else "없음"
    acf = f"{b['acf_bpm']:.2f}" if b.get("acf_bpm") is not None else "없음"
    label = "채택 후보" if selected >= 0 else "보류(진단값)"
    reasons = ", ".join(REASON_LABELS.get(r, r) for r in b.get("reason_names", []))
    suffix = f" | {reasons}" if reasons else ""
    print(f"{session} | {label} | SC {index} | PSD {psd} / ACF {acf} bpm{suffix}", flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode(raw):
    """Keep packet order and raw uint32 times; return text input and parse diagnostics."""
    packets, skipped = [], []
    layouts = set()
    previous = None
    span = wraps = duplicates = 0
    for line_no, line in enumerate(raw.splitlines(), 1):
        parsed = parse_line(line)
        if parsed.kind != "csi":
            continue
        if not parsed.parse_ok:
            skipped.append({"line": line_no, "reason": parsed.error})
            continue
        f = parsed.fields
        invalid = [key for key, (lo,hi) in LIMITS.items() if not lo <= int(f[key]) <= hi]
        gain = float(f["compensate_gain"])
        if not math.isfinite(gain) or abs(gain) > 3.4028234663852886e38:
            invalid.append("compensate_gain")
        if invalid:
            skipped.append({"line": line_no, "reason": "range:" + ",".join(invalid)})
            continue
        # A malformed CSI length would be rejected by C preprocessing too.
        length = int(f["len"])
        if length == 0 or length % 2:
            skipped.append({"line": line_no, "reason": "invalid_csi_length"})
            continue
        layouts.add((length, int(f["rx_format"]), int(f["channel"])))
        if len(layouts) != 1:
            raise ValueError(f"line {line_no}: CSI layout changed (len/format/channel)")
        stamp = int(f["local_timestamp"])
        if previous is not None:
            delta = (stamp-previous) & 0xffffffff
            if delta >= 2**31:
                raise ValueError(f"line {line_no}: timestamp moved backwards")
            span += delta
            if span >= 2**32:
                raise ValueError("session duration exceeds uint32 relative-time range")
            wraps += stamp < previous
            duplicates += delta == 0
        previous = stamp
        data = base64.b64decode(line.rsplit(b",", 1)[1], validate=True)
        signed = (str(v if v < 128 else v-256) for v in data)
        packets.append(" ".join(f[key] for key in FIELDS) + " " + " ".join(signed))
    if len(packets) < 2:
        raise ValueError("fewer than two usable records")
    return (str(len(packets))+"\n"+"\n".join(packets)+"\n"), {
        "decoded_records": len(packets), "skipped": skipped, "wraps": wraps,
        "duplicate_timestamps": duplicates, "input_duration_s": span/1e6,
        "layout": list(next(iter(layouts))),
    }


def run_session(folder, exe, method="amplitude"):
    source = folder / "serial.bin"
    metadata = folder / "session.json"
    paths = [source] + ([metadata] if metadata.exists() else [])
    before = {p.name: digest(p) for p in paths}
    report = {"session": folder.name, "source": str(source.resolve()), "source_sha256": before}
    try:
        if metadata.exists():
            meta = json.loads(metadata.read_text(encoding="utf-8"))
            report["conditions"] = meta.get("conditions", {})
            report["capture_status"] = meta.get("status")
        payload, parsed = decode(source.read_bytes())
        report["input"] = parsed
        if method == "phase" and parsed["layout"][:2] != [234, 2]:
            raise ValueError("phase requires the explicit 234-byte rx_format=2 legacy layout")
        command = [str(exe), "--stdin"] + (["--phase"] if method == "phase" else [])
        run = subprocess.run(command, input=payload, text=True,
                             capture_output=True, check=False)
        report.update(json.loads(run.stdout) if run.stdout.strip() else {"status": "process_error"})
        report["exit_code"] = run.returncode
        if run.returncode:
            report["status"] = "process_error"
            report["error"] = run.stderr.strip() or "C pipeline returned an error"
        for b in report.get("bins", []):
            b["reason_names"] = [name for bit,name in enumerate(REASONS) if b["reasons"] & (1 << bit)]
        if method == "phase":
            names = ("flat_signal", "insufficient_backup_sharpness", "spectral_peak_count_disagreement")
            report["reason_names"] = [name for bit,name in enumerate(names)
                                      if report.get("reasons", 0) & (1 << bit)]
    except (ValueError, OSError) as exc:
        report.update(status="input_error", error=str(exc))
    report["sources_unchanged"] = all(digest(p) == before[p.name] for p in paths)
    if not report["sources_unchanged"]:
        report.update(status="source_changed", error="source hash changed during processing")
    return report


def main():
    # Windows에서 파이프/로그로 실행할 때도 한글 결과를 UTF-8로 출력한다.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--session", action="append", help="session directory name (repeatable)")
    parser.add_argument("--output", type=Path, help="new output directory")
    parser.add_argument("--method", choices=("amplitude", "phase"), default="amplitude")
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    folders = [data_root / name for name in args.session] if args.session else sorted(
        p for p in data_root.iterdir() if (p / "serial.bin").is_file())
    for folder in folders:
        if folder.resolve().parent != data_root or not (folder / "serial.bin").is_file():
            parser.error(f"invalid session: {folder}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = (args.output or ROOT / f"outputs/c-{args.method}" / stamp).resolve()
    if output == data_root or data_root in output.parents:
        parser.error("output must be outside the source data directory")
    if output.exists():
        parser.error("output already exists; choose a new directory")
    build = ROOT / "build"
    subprocess.run(["cmake", "-S", str(ROOT), "-B", str(build)], check=True)
    subprocess.run(["cmake", "--build", str(build), "--target", "csi_resp_main"], check=True)
    exe = build / ("csi_resp_main.exe" if sys.platform == "win32" else "csi_resp_main")
    output.mkdir(parents=True)
    manifest = {"created_utc": stamp, "executable_sha256": digest(exe),
        "window": "full session", "gain_correction": "none; raw I/Q passed to current C preprocessing",
        "timestamp_policy": "original order, raw uint32, one wrap allowed; no gap compression",
        "config": {"interp_hz": 100, "target_hz": 10, "fir_half_s": 1, "kaiser_beta": 8,
                   "band_hz": [.05,.8], "min_duration_s": 20, "min_cycles": 3,
                   "min_concentration": .35, "min_acf": .3},
        "code_sha256": {str(p.relative_to(ROOT)): digest(p) for p in
            [*ROOT.glob("src/*.c"), *ROOT.glob("include/csi_resp/*.h"), Path(__file__)]}}
    manifest["method"] = args.method
    if args.method == "phase":
        manifest["gain_correction"] = "per-packet RMS normalization in C"
        manifest["config"] = {"layout_profile": "backup_ht40_117_assumed", "phase": "los_wls",
            "sample_interval": "median retained timestamp difference", "max_gap_s": .1,
            "min_duration_s": 20, "top_columns": 5, "band_hz": [.05, 1.], "frequency_step_hz": .005,
            "hampel": [11, 3], "butterworth_order": 4, "padding": "odd, 27 samples, untrimmed",
            "min_sharpness_exclusive": 1.55, "max_agreement_bpm_exclusive": 3.}
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    rows = []
    for folder in folders:
        report = run_session(folder, exe, args.method)
        (output/f"{folder.name}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        bins = report.get("bins", [])
        selected = report.get("selected_bin", -1)
        best = report.get("best_bin", -1)
        b = bins[best] if best >= 0 else {}
        conditions = report.get("conditions", {})
        row = {"session": folder.name, "distance_cm": conditions.get("distance_cm"),
               "occupancy": conditions.get("occupancy"), "status": report["status"],
               "selected_bin": selected, "candidate_bpm": bins[selected]["psd_bpm"] if selected >= 0 else None,
               "best_bin": best, "diagnostic_psd_bpm": b.get("psd_bpm"),
               "diagnostic_acf_bpm": b.get("acf_bpm"), "reasons": "|".join(b.get("reason_names", [])),
               "duration_s": report.get("duration_s"), "error": report.get("error", "")}
        if args.method == "phase":
            row = {"session": folder.name, "distance_cm": conditions.get("distance_cm"),
                "occupancy": conditions.get("occupancy"), "status": report["status"],
                "accepted": report.get("accepted", 0),
                "candidate_bpm": report.get("diagnostic_bpm") if report.get("accepted") else None,
                **{key: report.get(key) for key in ("diagnostic_bpm", "spectral_bpm", "peak_count_bpm",
                    "sharpness", "agreement_bpm", "n_peaks", "duration_s")},
                "reasons": "|".join(report.get("reason_names", [])), "error": report.get("error", "")}
        rows.append(row)
        print_result(report)
    if rows:
        with (output/"summary.csv").open("w",newline="",encoding="utf-8-sig") as f:
            writer=csv.DictWriter(f,fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    accepted = sum(r.get("accepted", r.get("selected_bin", -1) >= 0) for r in rows)
    errors = sum(r["status"] in ("input_error", "process_error", "source_changed") for r in rows)
    print(f"\n총 {len(rows)}개 | 채택 후보 {accepted}개 | 보류 {len(rows)-accepted-errors}개 | 오류 {errors}개")
    print(f"결과 저장: {output}")
    return int(any(r["status"] in ("input_error","process_error","source_changed") for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
