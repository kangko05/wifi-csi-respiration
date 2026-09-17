"""Hash every read-only source this run depends on, and re-check it afterwards.

Covers all files under the repository's `data/` and the legacy files the
comparison imports or executes (the vendored snapshot `vendor/wifi-csi-proto`,
whose source commit is read from its `VENDOR.json`). Run once with `--write`
before processing and once with `--check` after, so any change to an original
capture or to the legacy code during the run is caught instead of assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT.parent / "data"  # shared raw captures live at the repository root
LEGACY = (ROOT / "vendor" / "wifi-csi-proto").resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def legacy_sources() -> list[Path]:
    return [
        LEGACY / "scripts/compare_phase_cir.py",
        LEGACY / "scripts/assess_quality.py",
        LEGACY / "scripts/evaluate_band_limits.py",
        LEGACY / "main.py",
        LEGACY / "references/phase_cir_backup.json",
        LEGACY / "references/phase_cir_integration.md",
        *sorted((LEGACY / "src").rglob("*.py")),
    ]


def snapshot() -> dict:
    data = {str(p.relative_to(DATA_ROOT.parent)).replace("\\", "/"): sha256(p)
            for p in sorted(DATA_ROOT.rglob("*")) if p.is_file()}
    legacy = {str(p.relative_to(LEGACY)).replace("\\", "/"): sha256(p)
              for p in legacy_sources() if p.is_file()}
    try:
        commit = json.loads((LEGACY / "VENDOR.json").read_text(encoding="utf-8"))["source_git_commit"]
    except (OSError, ValueError, KeyError):  # pragma: no cover - environment
        commit = None
    return {
        "taken_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_root": str(DATA_ROOT),
        "legacy_root": str(LEGACY),
        "legacy_git_commit": commit,
        "n_data_files": len(data),
        "n_legacy_files": len(legacy),
        "data_sha256": data,
        "legacy_sha256": legacy,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, help="write a new snapshot JSON")
    parser.add_argument("--check", type=Path, help="compare against an earlier snapshot JSON")
    args = parser.parse_args(argv)
    if not args.write and not args.check:
        parser.error("choose --write or --check")

    current = snapshot()
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.write} ({current['n_data_files']} data files, "
              f"{current['n_legacy_files']} legacy files, legacy HEAD {current['legacy_git_commit']})")
    if args.check:
        previous = json.loads(args.check.read_text(encoding="utf-8"))
        problems = []
        for key in ("data_sha256", "legacy_sha256"):
            before, after = previous[key], current[key]
            for name in sorted(set(before) | set(after)):
                if name not in after:
                    problems.append(f"{key}: missing now: {name}")
                elif name not in before:
                    problems.append(f"{key}: appeared during run: {name}")
                elif before[name] != after[name]:
                    problems.append(f"{key}: content changed: {name}")
        if previous.get("legacy_git_commit") != current.get("legacy_git_commit"):
            problems.append("legacy git HEAD moved during the run")
        if problems:
            print("SOURCE VERIFICATION FAILED", file=sys.stderr)
            for problem in problems:
                print("  " + problem, file=sys.stderr)
            return 1
        print(f"sources unchanged: {current['n_data_files']} data files and "
              f"{current['n_legacy_files']} legacy files match {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
