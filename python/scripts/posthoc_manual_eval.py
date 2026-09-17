"""Post-hoc evaluation of the frozen legacy_run_20260917 predictions against manual counts.

The manual counts were supplied by the user after estimation. They are joined
to the already written `report/summary.csv` rows only; no estimator is run and
no decision, threshold or selection changes. The prediction files are hashed
before and after and the run fails if they change.

Interpretation (from the task, not independently confirmed by the user): each
value is a user-reported total breath count over the nominal 120 s capture
interval, not a synchronized reference. Occupied target bpm = count * 60 / 120.
The cropped per-method analysis span is never used as the denominator. A count
of 0 on an empty room is an occupancy-absence reference, never a 0 bpm target.
Sessions without a supplied count stay unavailable; nothing is invented.

Subcommands:
  make-reference  write references/manual_breath_counts_<date>.csv + provenance JSON
  evaluate        write a new posthoc_manual_<id>/ directory under the run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT.parent / "data"  # shared raw captures live at the repository root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from summarize_by_conditions import GROUP_ORDER, group_of  # noqa: E402

NOMINAL_INTERVAL_S = 120.0
METHODS = ("amplitude", "phase", "cir")
SOURCE_TEXT = "0 24 30 27 33 32 37 36 30 33 34 32 34 28 30 33 30 28 23"
INTERPRETATION = (
    "User-reported total breath count over the nominal 120 s full-capture interval, "
    "assigned in chronological session order (oldest first). Interval interpretation is "
    "inferred from the task; exact start/stop synchronization and counting uncertainty were "
    "not independently confirmed by the user. Not a synchronized reference."
)
REFERENCE_COLUMNS = (
    "chronological_index", "session_id", "source_token_index", "source_token",
    "count", "count_status", "occupancy", "distance_cm", "group",
    "nominal_interval_s", "reference_bpm", "reference_role",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_reference_rows(session_contexts: list[dict], source_text: str,
                         interval_s: float = NOMINAL_INTERVAL_S) -> list[dict]:
    """Map tokens to sessions in order; unmatched later sessions stay unavailable.

    `session_contexts` are chronological dicts with session_id/occupancy/
    distance_cm/group. Raises if tokens outnumber sessions or a token is not a
    non-negative integer, or if a zero lands on an occupied session / a nonzero
    count lands on an empty room.
    """
    tokens = source_text.split()
    if len(tokens) > len(session_contexts):
        raise ValueError("more counts than sessions")
    rows = []
    for index, context in enumerate(session_contexts):
        row = dict(chronological_index=index, session_id=context["session_id"],
                   occupancy=context["occupancy"], distance_cm=context["distance_cm"],
                   group=context["group"], nominal_interval_s=interval_s,
                   source_token_index="", source_token="", count="",
                   count_status="not_supplied", reference_bpm="", reference_role="unavailable")
        if index < len(tokens):
            token = tokens[index]
            if not token.isdigit():
                raise ValueError(f"token {index} is not a non-negative integer: {token!r}")
            count = int(token)
            empty = str(context["occupancy"]) == "0"
            if empty and count != 0:
                raise ValueError(f"nonzero count on empty room {context['session_id']}")
            if not empty and count == 0:
                raise ValueError(f"zero count on occupied session {context['session_id']}")
            row.update(source_token_index=index, source_token=token, count=count,
                       count_status="user_reported")
            if empty:
                row["reference_role"] = "occupancy_absence"
            else:
                row.update(reference_bpm=count * 60.0 / interval_s,
                           reference_role="occupied_rate")
        rows.append(row)
    return rows


def evaluate_rows(predictions: list[dict], references: dict[str, dict]) -> list[dict]:
    """Attach errors to frozen prediction rows. Uses reference_bpm only."""
    out = []
    for p in predictions:
        ref = references.get(p["session_id"], {})
        role = ref.get("reference_role", "unavailable")
        target = float(ref["reference_bpm"]) if role == "occupied_rate" else None
        diag = float(p["diagnostic_bpm"]) if p["diagnostic_bpm"] not in ("", "None") else None
        cand = float(p["candidate_bpm"]) if p["candidate_bpm"] not in ("", "None") else None
        res = float(p["resolution_bpm"]) if p["resolution_bpm"] not in ("", "None") else None
        row = dict(group=ref.get("group", ""), session_id=p["session_id"], method=p["method"],
                   status=p["status"], reference_role=role, count=ref.get("count", ""),
                   reference_bpm=target, diagnostic_bpm=diag, candidate_bpm=cand,
                   resolution_bpm=res, analysis_duration_s=p["analysis_duration_s"],
                   reasons=p["reasons"], diagnostic_error_bpm=None, accepted_error_bpm=None,
                   empty_room_accepted_candidate=None)
        if target is not None:
            if diag is not None:
                row["diagnostic_error_bpm"] = abs(diag - target)
            if cand is not None:
                row["accepted_error_bpm"] = abs(cand - target)
        elif role == "occupancy_absence" or (ref.get("group", "").startswith("empty")):
            row["empty_room_accepted_candidate"] = p["status"] == "accepted"
        out.append(row)
    return out


def _stats(rows: list[dict], key: str) -> dict:
    pairs = [(r[key], r["resolution_bpm"]) for r in rows if r[key] is not None]
    errors = [e for e, _ in pairs]
    return dict(
        n=len(errors),
        mae=statistics.fmean(errors) if errors else None,
        median=statistics.median(errors) if errors else None,
        max=max(errors) if errors else None,
        within_1_1=sum(e <= 1.1 for e in errors),
        within_2_2=sum(e <= 2.2 for e in errors),
        within_resolution=sum(res is not None and e <= res for e, res in pairs),
    )


def summarize(rows: list[dict]) -> list[dict]:
    result = []
    groups = [g for g in GROUP_ORDER if any(r["group"] == g for r in rows)]
    for group in [*groups, "all_occupied"]:
        for method in METHODS:
            if group == "all_occupied":
                subset = [r for r in rows if r["method"] == method
                          and r["reference_role"] == "occupied_rate"]
            else:
                subset = [r for r in rows if r["method"] == method and r["group"] == group]
            entry = dict(group=group, method=method, n_sessions=len(subset),
                         n_with_rate_reference=sum(r["reference_role"] == "occupied_rate" for r in subset),
                         n_accepted=sum(r["status"] == "accepted" for r in subset),
                         n_withheld=sum(r["status"] == "withheld" for r in subset))
            if entry["n_with_rate_reference"]:
                entry["diagnostic"] = _stats(subset, "diagnostic_error_bpm")
                entry["accepted"] = _stats(subset, "accepted_error_bpm")
            else:
                entry["empty_room_accepted_candidates"] = sum(
                    bool(r["empty_room_accepted_candidate"]) for r in subset)
                entry["reference_roles"] = sorted({r["reference_role"] for r in subset})
            result.append(entry)
    return result


def _contexts(run: Path) -> list[dict]:
    contexts = []
    for path in sorted((run / "derived").iterdir()):
        info = json.loads((path / "session.json").read_text(encoding="utf-8"))
        cond = info["derived_from"]["source_conditions"] or {}
        interrupted = bool((info.get("annotations") or {}).get("operator_interrupted"))
        contexts.append(dict(session_id=path.name, occupancy=cond.get("occupancy"),
                             distance_cm=cond.get("distance_cm"),
                             group=group_of(cond, interrupted)))
    return contexts


def cmd_make_reference(args) -> int:
    run = args.run.resolve()
    contexts = _contexts(run)
    data_ids = sorted(p.name for p in DATA_ROOT.iterdir() if p.is_dir())
    if [c["session_id"] for c in contexts] != data_ids:
        raise SystemExit("derived session list does not match data/ chronological order")
    rows = build_reference_rows(contexts, SOURCE_TEXT)
    out = args.output.resolve()
    with out.open("x", encoding="utf-8", newline="") as handle:  # never overwrite
        writer = csv.DictWriter(handle, fieldnames=REFERENCE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    provenance = dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        source_text_verbatim=SOURCE_TEXT, n_tokens=len(SOURCE_TEXT.split()),
        supplied_by="user, after estimation of legacy_run_20260917",
        supplied_after_estimation=True, order="chronological by session_id (oldest first)",
        interpretation=INTERPRETATION, nominal_interval_s=NOMINAL_INTERVAL_S,
        target_formula="reference_bpm = count * 60 / 120 for occupied sessions only",
        empty_room_policy="count 0 = occupancy-absence reference; never a 0 bpm regression target",
        missing=[r["session_id"] for r in rows if r["count_status"] == "not_supplied"],
        missing_policy="no count supplied; left unavailable, nothing invented",
        legacy_reference_labels_used=False,
        csv_path=str(out), csv_sha256=sha256(out),
    )
    prov_path = out.with_suffix(".provenance.json")
    with prov_path.open("x", encoding="utf-8") as handle:
        json.dump(provenance, handle, indent=2, ensure_ascii=False)
    print(f"wrote {out} ({len(rows)} rows, {provenance['n_tokens']} counts) and {prov_path}")
    return 0


def _fmt(value, digits=3):
    return "—" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def write_markdown(path: Path, summary: list[dict], rows: list[dict], meta: dict) -> None:
    lines = [
        "# 사후 수동 기준 평가 · legacy_run_20260917 (추정 재실행 없음)", "",
        f"- 기준: `{meta['reference_csv']}` — 사용자 보고 총 호흡 횟수, 명목 120초 구간 가정, 동기화 미확인",
        "- 목표 bpm = 횟수/2 (재실 세션만). 경로별 잘린 분석 길이는 분모로 쓰지 않음",
        "- 빈 방 횟수0은 부재 기준이며 0bpm 목표가 아님. 마지막 빈 방 2개는 횟수 미제공",
        f"- 고정 예측 `report/summary.csv` sha256 전후 동일: `{meta['predictions_sha256_before']}`",
        "- 진단 = 보류 포함 모든 진단 피크, 채택 = 채택된 후보만. 분모는 각 칸의 n", "",
        "| 그룹 | 방식 | 세션 | 채택 | 진단 n / MAE / ±1.1 / ±2.2 / ≤1칸 | 채택 n / MAE / ±1.1 / ±2.2 / ≤1칸 | 빈 방 채택 후보 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in summary:
        if "diagnostic" in e:
            d, a = e["diagnostic"], e["accepted"]
            dcell = f"{d['n']} / {_fmt(d['mae'])} / {d['within_1_1']} / {d['within_2_2']} / {d['within_resolution']}"
            acell = f"{a['n']} / {_fmt(a['mae'])} / {a['within_1_1']} / {a['within_2_2']} / {a['within_resolution']}"
            ecell = "—"
        else:
            dcell = acell = "기준 없음(빈 방)"
            ecell = f"{e['empty_room_accepted_candidates']} / {e['n_sessions']}"
        lines.append(f"| {e['group']} | {e['method']} | {e['n_sessions']} | {e['n_accepted']} | {dcell} | {acell} | {ecell} |")
    lines += ["", "## 세션별", "",
              "| 그룹 | 세션 | 방식 | 상태 | 횟수 | 목표 bpm | 진단 bpm | 진단 오차 | 채택 bpm | 채택 오차 | 1칸 bpm |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['group']} | `…_{r['session_id'][-8:]}` | {r['method']} | {r['status']} | "
                     f"{r['count'] if r['count'] != '' else '미제공'} | {_fmt(r['reference_bpm'], 2)} | "
                     f"{_fmt(r['diagnostic_bpm'], 2)} | {_fmt(r['diagnostic_error_bpm'], 2)} | "
                     f"{_fmt(r['candidate_bpm'], 2)} | {_fmt(r['accepted_error_bpm'], 2)} | {_fmt(r['resolution_bpm'], 4)} |")
    lines += ["", "## 해석 한계", "",
              "- 사후 1회 대조이며 임계값·선택은 실행 시 고정값 그대로다. 같은 데이터로 튜닝하지 않았다.",
              "- 수동 기준의 시작·끝 동기화와 세는 불확실성은 확인되지 않았다. 정밀 기준이 아니다.",
              "- 완전 수집 빈 방 2개 모두 phase·cir이 후보를 채택했다. 현재 게이트는 부재 판정으로 검증되지 않았다.",
              "- 이번 묶음에서 500cm 주기가 맞았다는 관측이며, 케이스 교체의 인과 효과나 일반적인 5m 정확도 증거가 아니다.",
              "- phase와 cir은 전처리를 공유하므로 두 경로의 일치는 독립 증거가 아니다."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_evaluate(args) -> int:
    run = args.run.resolve()
    predictions_path = run / "report/summary.csv"
    frozen = [predictions_path, run / "report/manifest.json", run / "report/evaluation.json"]
    before = {p.relative_to(run).as_posix(): sha256(p) for p in frozen}
    reference_path = args.reference.resolve()
    output = run / f"posthoc_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output.mkdir(exist_ok=False)

    with reference_path.open(encoding="utf-8", newline="") as handle:
        references = {r["session_id"]: r for r in csv.DictReader(handle)}
    with predictions_path.open(encoding="utf-8-sig", newline="") as handle:
        predictions = list(csv.DictReader(handle))
    if len(predictions) != 63 or set(references) != {p["session_id"] for p in predictions}:
        raise SystemExit("prediction rows or session ids do not match the reference table")

    rows = evaluate_rows(predictions, references)
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda r: (order.get(r["group"], 99), r["session_id"], METHODS.index(r["method"])))
    summary = summarize(rows)

    after = {p.relative_to(run).as_posix(): sha256(p) for p in frozen}
    if after != before:
        raise SystemExit("frozen prediction files changed during evaluation")
    meta = dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(), run=str(run),
        reference_csv=str(reference_path.relative_to(ROOT)).replace("\\", "/"),
        reference_sha256=sha256(reference_path),
        frozen_sha256_before=before, frozen_sha256_after=after,
        predictions_sha256_before=before["report/summary.csv"],
        estimator_rerun=False, thresholds_changed=False, labels_supplied_after_estimation=True,
        target_formula="count * 60 / 120 (nominal manual interval), occupied only",
        tolerances_bpm=[1.1, 2.2], resolution_threshold="per-row resolution_bpm from summary.csv",
        evaluator_sha256=sha256(Path(__file__)), interpretation=INTERPRETATION,
    )
    with (output / "posthoc_rows.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (output / "posthoc_summary.json").open("x", encoding="utf-8") as handle:
        json.dump(dict(meta=meta, summary=summary), handle, indent=2, ensure_ascii=False)
    write_markdown(output / "report.md", summary, rows, meta)
    for e in summary:
        if "diagnostic" in e:
            d, a = e["diagnostic"], e["accepted"]
            print(f"{e['group']:<14} {e['method']:<9} acc={e['n_accepted']}/{e['n_sessions']} "
                  f"diag n={d['n']} mae={_fmt(d['mae'])} 1.1={d['within_1_1']} 2.2={d['within_2_2']} res={d['within_resolution']} | "
                  f"acc n={a['n']} mae={_fmt(a['mae'])} 1.1={a['within_1_1']} 2.2={a['within_2_2']} res={a['within_resolution']}")
        else:
            print(f"{e['group']:<14} {e['method']:<9} empty accepted candidates={e['empty_room_accepted_candidates']}/{e['n_sessions']}")
    print("Wrote", output)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make-reference")
    make.add_argument("--run", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    ev = sub.add_parser("evaluate")
    ev.add_argument("--run", type=Path, required=True)
    ev.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args(argv)
    return cmd_make_reference(args) if args.command == "make-reference" else cmd_evaluate(args)


if __name__ == "__main__":
    raise SystemExit(main())
