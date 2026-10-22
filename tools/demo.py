"""The demo in three commands, on top of the recorder (tools/record_main.py) that owns the port.

  calibrate <session> [--minutes 3] [--out-dir baselines]
      Quiet baseline for the Breathing judge (bedroom Main path, `breathing baseline-live`),
      then Presence thresholds from the same minutes (`presence thresholds`). Keep the room
      empty and the doll off for the whole time. The baseline goes to baselines/1_2.json, the
      file `breathing live` reads by default.
  judge <session> [--baselines-dir baselines] [--port 8090] [--voice-server http://127.0.0.1:8765]
      Start the Breathing live judge, the Presence live judge, the phone page and the bridge that
      sends the home state to the voice_box server as detached processes; their PIDs go to
      <session>/demo_pids.txt. The page runs with --no-apnea-hold: nobody moves in the doll's
      room, so an APNEA is shown as APNEA.
  stop <session>
      Stop the processes `judge` started. The recorder keeps running.

Every subprocess is `python -X utf8 -m ...` from the repository root, so this file is only a
sequence of the commands docs/runbook-lab.md lists.
"""

import argparse
import datetime as dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = [sys.executable, "-X", "utf8"]
PIDS_FILE = "demo_pids.txt"
BAND = ["--band", "10", "20"]
WINDOW_S = 60.0


def calibrate(session: Path, minutes: float, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    breathing_out = out_dir / "1_2.json"
    presence_out = out_dir / "presence.json"
    start = time.time()
    code = subprocess.call(
        PYTHON
        + [
            "-m",
            "breathing",
            "baseline-live",
            str(session),
            "--room",
            "1",
            "--node",
            "2",
            "--minutes",
            str(minutes),
            "--out",
            str(breathing_out),
        ]
        + BAND,
        cwd=ROOT,
    )
    end = time.time()

    if code != 0:
        print(
            f"breathing baseline failed (exit {code}); fix the input first",
            file=sys.stderr,
        )
        return code

    # The Presence windows must lie whole inside the interval, so the first window ends a minute in.
    if end - start < 2 * WINDOW_S:
        print(
            f"presence thresholds need at least 2 minutes of empty room, got {end - start:.0f} s",
            file=sys.stderr,
        )
        return 1

    interval = f"{_hms(start)}-{_hms(end)}"
    print(f"presence thresholds from {interval}", file=sys.stderr)
    code = subprocess.call(
        PYTHON
        + [
            "-m",
            "presence",
            "thresholds",
            str(session),
            "--empty",
            interval,
            "--out",
            str(presence_out),
        ]
        + BAND,
        cwd=ROOT,
    )

    if code != 0:
        print(f"presence thresholds failed (exit {code})", file=sys.stderr)
        return code

    print(f"calibrated: {breathing_out} and {presence_out}", file=sys.stderr)

    return 0


def judge(session: Path, baselines_dir: Path, port: int, voice_server: str) -> int:
    breathing_baseline = baselines_dir / "1_2.json"
    presence_thresholds = baselines_dir / "presence.json"

    for path in (breathing_baseline, presence_thresholds):
        if not path.exists():
            print(f"missing {path}: run calibrate first", file=sys.stderr)
            return 2

    if (session / PIDS_FILE).exists():
        print(f"{session / PIDS_FILE} exists: run stop first", file=sys.stderr)
        return 2

    commands = {
        "breathing": (
            [
                "-m",
                "breathing",
                "live",
                str(session),
                "--room",
                "1",
                "--node",
                "2",
                "--baseline-file",
                str(breathing_baseline),
            ]
            + BAND,
            session / "live_screen.txt",
        ),
        "presence": (
            [
                "-m",
                "presence",
                "live",
                str(session),
                "--thresholds",
                str(presence_thresholds),
                "--home-rule",
                "motion",
                "--empty-minutes",
                "1.5",
            ]
            + BAND,
            session / "presence_live.log",
        ),
        "page": (
            [
                "tools/marker_server.py",
                str(session),
                "--port",
                str(port),
                "--no-apnea-hold",
            ],
            session / "marker_server.log",
        ),
        "bridge": (
            ["tools/presence_bridge.py", str(session), "--server", voice_server],
            session / "presence_bridge.log",
        ),
    }
    pids = []

    for name, (args, log) in commands.items():
        process = _spawn(PYTHON + args, log)
        pids.append(f"{name}\t{process.pid}")
        print(f"{name}: pid {process.pid}, log {log}", file=sys.stderr)

    (session / PIDS_FILE).write_text("\n".join(pids) + "\n", encoding="utf-8")
    time.sleep(3)
    alive = [line for line in pids if _alive(int(line.split("\t")[1]))]

    if len(alive) < len(pids):
        print("a process exited at once; read its log", file=sys.stderr)
        return 1

    print(
        f"page: http://<this PC's IP>:{port}/  ticks: {session / 'ticks.jsonl'}, {session / 'presence_ticks.jsonl'}",
        file=sys.stderr,
    )
    return 0


def stop(session: Path) -> int:
    path = session / PIDS_FILE

    if not path.exists():
        print("nothing to stop", file=sys.stderr)
        return 0

    for line in path.read_text(encoding="utf-8").splitlines():
        name, pid = line.split("\t")
        _kill(int(pid))
        print(f"stopped {name} ({pid})", file=sys.stderr)

    path.unlink()
    return 0


def _spawn(args: list[str], log: Path) -> subprocess.Popen:
    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if os.name == "nt"
        else 0
    )
    return subprocess.Popen(
        args,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=open(log, "ab"),
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def _alive(pid: int) -> bool:
    if os.name == "nt":
        # tasklist prints in the console code page, not UTF-8, so compare bytes.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True
        ).stdout
        return str(pid).encode() in out

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


def _hms(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    cal = commands.add_parser("calibrate")
    cal.add_argument("session", type=Path)
    cal.add_argument("--minutes", type=float, default=3.0)
    cal.add_argument("--out-dir", type=Path, default=ROOT / "baselines")

    jud = commands.add_parser("judge")
    jud.add_argument("session", type=Path)
    jud.add_argument("--baselines-dir", type=Path, default=ROOT / "baselines")
    jud.add_argument("--port", type=int, default=8090)
    jud.add_argument("--voice-server", default="http://127.0.0.1:8765")

    stp = commands.add_parser("stop")
    stp.add_argument("session", type=Path)

    args = parser.parse_args()

    if args.command == "calibrate":
        return calibrate(args.session, args.minutes, args.out_dir)
    if args.command == "judge":
        return judge(args.session, args.baselines_dir, args.port, args.voice_server)
    return stop(args.session)


if __name__ == "__main__":
    raise SystemExit(main())
