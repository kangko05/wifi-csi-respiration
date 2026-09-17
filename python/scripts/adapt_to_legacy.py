"""Write legacy-readable derived copies of this project's captures.

Reads `data/<session>/{serial.bin,lines.csv,session.json}` and writes
`<output>/derived/<session>/{csi_raw.npy,meta.csv,session.json}` plus the
`rowmap.csv`, `excluded_rows.csv` and `adapter_sidecar.json` provenance files.

The originals are opened read-only. Nothing under `data/` is written, moved or
renamed, and the original manifests are never edited: operator notes that were
established after the capture (for example an interrupted run) are recorded in
the derived `session.json` `annotations` block only.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT.parent / "data"  # shared raw captures live at the repository root
sys.path.insert(0, str(ROOT / "src"))

from csi_adapt.legacy_format import ADAPTER_VERSION, convert_session, sha256_file  # noqa: E402

# Operator statements recorded after capture. These annotate derived data only;
# the original `data/.../session.json` still says what the collector wrote.
ANNOTATIONS: dict[str, dict] = {
    "20260916T063413_795063_e446ed62": {
        "operator_interrupted": True,
        "source_manifest_status": "complete",
        "note": (
            "User confirmed they interrupted this capture. The original manifest says "
            "'complete' and is left unchanged. CSI stops at 94.033377 s of host elapsed time; "
            "the 216.791913 s host span covers a 122.758536 s tail with no CSI line, ending in "
            "a single trailing partial byte. Only the ~94 s of actually received CSI is "
            "analysable; the 216.8 s span is not a valid CSI duration and the tail is not filled."
        ),
        "notes": [
            "operator-interrupted capture; analyse the received ~94 s only",
            "do not report 216.8 s as CSI duration",
        ],
    },
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DATA_ROOT)
    parser.add_argument("--output", type=Path, required=True,
                        help="new run directory under this project's outputs/")
    args = parser.parse_args(argv)

    source = args.path.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error("input must be a directory of capture directories")
    if not output.is_relative_to(ROOT / "outputs") or output == ROOT / "outputs":
        parser.error("output must be a new directory under this project's outputs/")
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("output must not overlap the input")

    sessions = sorted(p for p in source.iterdir() if p.is_dir())
    if not sessions:
        parser.error("no capture directories found")

    derived = output / "derived"
    derived.mkdir(parents=True, exist_ok=False)

    reports = []
    for path in sessions:
        report = convert_session(path, derived / path.name,
                                 annotations=ANNOTATIONS.get(path.name, {}))
        reports.append(report.to_json())
        print(f"{path.name}: kept={report.n_kept_packets} excluded={report.n_excluded_rows} "
              f"timestamp_policy={report.timestamp_policy['policy']} "
              f"span_s={report.interval_us['span_s']:.6f}", flush=True)

    manifest = {
        "artifact_kind": ADAPTER_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source),
        "derived_root": str(derived),
        "n_sessions": len(reports),
        "n_kept_packets_total": sum(r["n_kept_packets"] for r in reports),
        "n_excluded_rows_total": sum(r["n_excluded_rows"] for r in reports),
        "host": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "argv": [str(a) for a in (argv if argv is not None else sys.argv)],
        },
        "adapter_code_sha256": {
            str(p): sha256_file(p)
            for p in (Path(__file__), ROOT / "src/csi_adapt/legacy_format.py",
                      ROOT / "src/csi_adapt/__init__.py")
        },
        "sessions": reports,
    }
    (output / "adapter_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print("Adapter manifest:", output / "adapter_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
