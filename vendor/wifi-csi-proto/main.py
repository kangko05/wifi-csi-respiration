"""CSI recording and existing offline analysis entry point."""

from __future__ import annotations

import argparse
from datetime import datetime
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from csi_pipeline.capture import CaptureConfig, capture_session, save_evaluation


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ESP32-C5 CSI: ports / record / analyze / compare")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="List serial ports without opening them")
    record = commands.add_parser("record", help="Record raw CSI; ask for count after STOP")
    record.add_argument("--port", default="COM3")
    record.add_argument("--baud", type=int, default=921600)
    record.add_argument("--duration", type=positive_float, default=120.0, metavar="SECONDS")
    record.add_argument("--delay", type=nonnegative_float, default=10.0, metavar="SECONDS")
    record.add_argument("--idle-timeout", type=positive_float, default=5.0)
    record.add_argument("--name", help="New session folder name; default timestamp")
    record.add_argument("--data-dir", type=Path, default=ROOT / "data")
    record.add_argument("--preset", choices=["lab-split"], help="Current occupied lab, TX-RX ~80cm, person-RX ~10cm")
    record.add_argument("--tx-rx-cm", type=positive_float)
    record.add_argument("--person-rx-cm", type=nonnegative_float)
    record.add_argument("--person-position", help="Person position relative to TX/RX")
    record.add_argument("--setting", help="Location and other people/interference")
    record.add_argument("--note", default="")
    record.add_argument("--no-prompt", action="store_true", help="Skip post-capture manual count questions")
    analyze = commands.add_parser("analyze", help="Existing amplitude baseline, whole session, unchanged thresholds")
    analyze.add_argument("session", type=Path)
    compare = commands.add_parser("compare", help="Independent amplitude / phase / CIR decisions on the same captures")
    compare.add_argument("path", type=Path, help="One session or a parent containing sessions")
    compare.add_argument("--methods", nargs="+", choices=("amplitude", "phase", "cir"), default=["amplitude", "phase", "cir"])
    compare.add_argument("--include-legacy", action="store_true", help="Also reproduce the backup's combined decision")
    compare.add_argument("--reference-csv", type=Path, help="Optional post-estimation evaluation only")
    compare.add_argument("--output", type=Path, help="New directory under outputs; default timestamp")
    return parser


def capture_context(args: argparse.Namespace) -> dict:
    context = dict(
        preset=args.preset, port=args.port, baud=args.baud, countdown_s=args.delay,
        tx_rx_cm=None, person_rx_cm=None, person_position=None, setting=None,
        distance_precision="approximate", note=args.note,
    )
    if args.preset == "lab-split":
        context.update(
            tx_rx_cm=80.0, person_rx_cm=10.0,
            person_position="between TX and RX, near RX",
            setting="occupied laboratory; other people not nearby; possible movement/RF interference",
        )
    for key in ("tx_rx_cm", "person_rx_cm", "person_position", "setting"):
        if getattr(args, key) is not None:
            context[key] = getattr(args, key)
    return context


def prompt_evaluation(path: Path) -> None:
    while True:
        value = input("호흡 횟수 (모르면 Enter): ").strip()
        if not value or (value.isascii() and value.isdecimal()):
            break
        print("0 이상의 정수 또는 Enter를 입력하세요.")
    count = int(value) if value else None
    confidence = input("횟수가 확실하면 y, 불확실하면 Enter: ").strip().lower()
    note = input("본인/주변 사람 움직임·특이사항 (없으면 Enter): ").strip()
    save_evaluation(path, count, count is None or confidence != "y", note)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            command = [sys.executable, "-B", str(ROOT / "scripts" / "compare_phase_cir.py"),
                       str(args.path.resolve()), "--methods", *args.methods]
            if args.include_legacy:
                command.append("--include-legacy")
            if args.reference_csv:
                command.extend(["--reference-csv", str(args.reference_csv.resolve())])
            if args.output:
                command.extend(["--output", str(args.output.resolve())])
            return subprocess.run(command, cwd=ROOT, check=False).returncode
        if args.command == "analyze":
            session = args.session.resolve()
            if not (session / "session.json").is_file():
                parser.error("session must be a capture folder containing session.json")
            output = ROOT / "outputs" / "recordings" / session.name / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            output.mkdir(parents=True, exist_ok=False)
            return subprocess.run([
                sys.executable, "-B", str(ROOT / "scripts" / "estimate_periodicity.py"),
                str(session), "--full-session-only", "--gain-method", "none", "--output", str(output),
            ], cwd=ROOT, check=False).returncode
        try:
            import serial
            from serial.tools import list_ports
        except ImportError:
            print('Install: .\\.venv\\Scripts\\python.exe -m pip install -e ".[capture]"', file=sys.stderr)
            return 1
        if args.command == "ports":
            ports = sorted(list_ports.comports(), key=lambda p: p.device)
            for port in ports:
                print(f"{port.device}: {port.description}")
            if not ports:
                print("No serial ports found.")
            return 0
        if args.baud <= 0:
            parser.error("--baud must be positive")
        name = args.name or "capture_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if name in (".", "..") or any(c in name for c in '<>:"/\\|?*') or name.rstrip(" .") != name:
            parser.error("--name must be a single valid folder name")
        path = args.data_dir.resolve() / name
        if path.exists():
            parser.error(f"Session already exists; choose another --name: {path}")
        context = capture_context(args)
        print(f"RX {args.port}, {args.baud} baud | {args.duration:g}s | {path}", flush=True)
        print(f"배치: {context}", flush=True)
        # Set control lines before opening; do not deliberately reset the board.
        stream = serial.Serial(port=None, baudrate=args.baud, timeout=0.2)
        stream.dtr = False
        stream.rts = False
        stream.port = args.port
        with stream:
            remaining = args.delay
            while remaining > 0:
                print(f"시작까지 {remaining:g}초", flush=True)
                step = min(1.0, remaining)
                time.sleep(step)
                remaining -= step
            stream.reset_input_buffer()
            result = capture_session(
                stream, path, CaptureConfig(args.duration, args.idle_timeout), context,
                notify=lambda message: print(message, flush=True),
            )
        print(f"저장: {path}\n상태: {result.status}, CSI {result.n_kept}개, 입력 검증: {result.input_valid}", flush=True)
        if result.error:
            print(result.error, file=sys.stderr)
        if result.n_kept and not args.no_prompt:
            try:
                prompt_evaluation(path)
            except (EOFError, KeyboardInterrupt):
                print("\n수동 횟수 입력 생략. 원본 수집 파일은 저장돼 있습니다.")
        if result.input_valid:
            print(f'분석: .\\.venv\\Scripts\\python.exe main.py analyze "{path}"')
            print(f'진폭/위상/CIR 비교: .\\.venv\\Scripts\\python.exe main.py compare "{path}"')
        return 0 if result.status == "complete" and result.input_valid else 1
    except KeyboardInterrupt:
        print("\n시작 전 취소했습니다.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
