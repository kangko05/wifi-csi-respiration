"""Demo page: three buttons (calibrate, start, stop) and the Breathing state, in a browser.

usage: demo_ui.py [NAME] [--node 1] [--minutes 3] [--start-delay 0] [--udp-port 3334]
                  [--serial SERIAL] [--fake] [--port 8080] [--baselines-dir baselines]

  calibrate  Opens a recording and runs `breathing baseline-live` on it for --minutes (room 1,
             --node) after --start-delay seconds (time to leave); the page counts down. Keep the
             room empty and the doll off. The baseline
             goes to baselines/1_<node>.json and the recording closes when it is done.
  start      Opens a new recording and runs `breathing live` on it with that baseline; the page
             shows the judge's latest state.
  stop       Ends whatever runs: the judge and its recording, or a calibration early (the
             baseline is then made from the windows scored so far).

Each recording is a session under recordings/<stamp>_<NAME> or <NAME>_calibrate. The recorder
is tools/record_udp.py on --udp-port (the case RX on relay=3 sends over the AP), or with
--serial tools/record_main.py on that board (the main ESP's UART). --fake replaces it with
tools/fake_stream.py --realtime, to rehearse the page without boards.

Open http://<this PC>:8080/ on the laptop or a phone on the same network. Ctrl+C stops the page
and anything it started.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo import BAND, PYTHON, ROOT, _copy_lines, _kill, _spawn  # noqa: E402
from marker_server import BREATHING_STATE, lan_address  # noqa: E402

RECORDINGS_DIR = ROOT / "recordings"
TICKS_FILE = "ticks.jsonl"
TICK_STALE_S = 40.0
RECORDER_START_S = 10.0


class Phase(Enum):
    IDLE = "idle"
    CALIBRATING = "calibrating"
    RUNNING = "running"


@dataclass(frozen=True)
class RecorderSpec:
    """How a recording is opened. `fake` writes a synthetic session instead of recording boards."""

    udp_port: int
    serial_number: str | None
    fake: bool
    node: int  # the fake stream writes this node; real recorders take whatever arrives


@dataclass
class Run:
    """One phase in flight: a recorder writing `session` and the worker (baseline-live or live)
    reading it."""

    session: Path
    recorder: subprocess.Popen
    worker: subprocess.Popen | None  # None while a calibration waits out the start delay
    deadline: float | None  # calibration end, None for a judge run


@dataclass
class Demo:
    """Everything the page controls. `lock` guards `phase`, `run` and `message`: the HTTP handler
    threads and the thread that waits for a calibration all touch them."""

    name: str
    node: int
    minutes: float
    start_delay_s: float  # time to leave the room before the baseline starts
    baselines_dir: Path
    recorder_spec: RecorderSpec
    lock: threading.Lock
    phase: Phase = Phase.IDLE
    run: Run | None = None
    message: str = ""

    @property
    def baseline_file(self) -> Path:
        return self.baselines_dir / f"1_{self.node}.json"

    # ------------------------------------------------------------- buttons

    def calibrate(self) -> str:
        with self.lock:
            if self.phase is not Phase.IDLE:
                return f"지금은 {self.phase.value} 중"

            try:
                recorder, session = start_recorder(f"{self.name}_calibrate", self.recorder_spec)
            except RecorderError as error:
                self.message = f"녹화를 시작하지 못했습니다: {error}"
                return self.message

            self.baselines_dir.mkdir(parents=True, exist_ok=True)
            self.run = Run(session, recorder, None, time.time() + self.start_delay_s + self.minutes * 60.0)
            self.phase = Phase.CALIBRATING
            self.message = f"{self.start_delay_s:.0f}초 뒤 기준선 측정 {self.minutes:g}분: 방을 비우고 인형을 끈 채로 둡니다"

        threading.Thread(target=self._wait_calibration, args=(self.run,), daemon=True).start()
        return self.message

    def _wait_calibration(self, run: Run) -> None:
        """Waits out the start delay, runs baseline-live, closes the recording. A stop during
        the delay ends the run without a baseline."""
        delay_end = time.time() + self.start_delay_s

        while time.time() < delay_end:
            time.sleep(0.2)

            with self.lock:
                if self.run is not run:
                    stop_recorder(run.recorder)
                    return

        args = [
            "-m", "breathing", "baseline-live", str(run.session),
            "--room", "1", "--node", str(self.node),
            "--minutes", str(self.minutes),
            "--out", str(self.baseline_file),
        ] + BAND

        with self.lock:
            if self.run is not run:
                stop_recorder(run.recorder)
                return

            run.worker = _spawn(PYTHON + args, run.session / "calibrate.log")
            self.message = f"기준선 측정 {self.minutes:g}분"

        code = run.worker.wait()
        stop_recorder(run.recorder)

        with self.lock:
            if self.run is not run:
                return

            self.run = None
            self.phase = Phase.IDLE

            if code == 0:
                self.message = "기준선 완료"
            else:
                self.message = f"기준선 실패 (exit {code}): {run.session / 'calibrate.log'}"

    def start(self) -> str:
        with self.lock:
            if self.phase is not Phase.IDLE:
                return f"지금은 {self.phase.value} 중"

            if not self.baseline_file.exists():
                return f"기준선이 없습니다 ({self.baseline_file.name}): calibrate 먼저"

            try:
                recorder, session = start_recorder(self.name, self.recorder_spec)
            except RecorderError as error:
                self.message = f"녹화를 시작하지 못했습니다: {error}"
                return self.message

            args = [
                "-m", "breathing", "live", str(session),
                "--room", "1", "--node", str(self.node),
                "--baseline-file", str(self.baseline_file),
            ] + BAND
            worker = _spawn(PYTHON + args, session / "live_screen.txt")
            self.run = Run(session, recorder, worker, None)
            self.phase = Phase.RUNNING
            self.message = f"녹화·판정 중: {session.name}"

        return self.message

    def stop(self) -> str:
        with self.lock:
            if self.phase is Phase.CALIBRATING:
                if self.run.worker is None:
                    # Still in the start delay: the waiting thread sees run change and closes the recorder.
                    self.run = None
                    self.phase = Phase.IDLE
                    self.message = "기준선 측정 취소"
                    return self.message

                # The waiting thread closes the recorder and resets the phase once the worker exits.
                interrupt(self.run.worker)
                return "기준선 측정을 끊습니다"

            if self.phase is not Phase.RUNNING:
                return "돌고 있는 것이 없습니다"

            self._end_run()
            self.message = "멈춤"

        return self.message

    def _end_run(self) -> None:
        if self.run.worker.poll() is None:
            _kill(self.run.worker.pid)
            self.run.worker.wait()

        stop_recorder(self.run.recorder)
        self.run = None
        self.phase = Phase.IDLE

    # -------------------------------------------------------------- status

    def status(self) -> dict:
        now = time.time()

        with self.lock:
            if self.phase is Phase.RUNNING and self.run.worker.poll() is not None:
                code = self.run.worker.returncode
                log = self.run.session / "live_screen.txt"
                self._end_run()
                self.message = f"판정 프로세스가 끝났습니다 (exit {code}): {log}"

            running = self.run is not None
            baseline_at = self.baseline_file.stat().st_mtime if self.baseline_file.exists() else None

            return {
                "now": now,
                "name": self.name,
                "node": self.node,
                "phase": self.phase.value,
                "message": self.message,
                "session": self.run.session.name if running else None,
                "remaining_s": max(0.0, self.run.deadline - now) if running and self.run.deadline else None,
                "recorder_alive": running and self.run.recorder.poll() is None,
                "baseline_at": baseline_at,
                "breathing": breathing_view(self.run.session, now) if self.phase is Phase.RUNNING else None,
            }

    def close(self) -> None:
        with self.lock:
            if self.phase is Phase.CALIBRATING:
                if self.run.worker is not None:
                    interrupt(self.run.worker)
                    self.run.worker.wait()

                stop_recorder(self.run.recorder)
                self.run = None
                self.phase = Phase.IDLE
            elif self.phase is Phase.RUNNING:
                self._end_run()


def interrupt(process: subprocess.Popen) -> None:
    """Ctrl+C for baseline-live, which then writes the baseline from the windows it has so far.
    Windows has no SIGINT for another process, so there the run is killed and no baseline is
    written (unverified on Windows)."""
    if os.name == "nt":
        _kill(process.pid)
    else:
        process.send_signal(signal.SIGINT)


# ---------------------------------------------------------------- recorder

class RecorderError(Exception):
    pass


def start_recorder(name: str, spec: RecorderSpec) -> tuple[subprocess.Popen, Path]:
    """Opens a recording and returns the recorder with the session folder it writes."""
    if spec.fake:
        session = RECORDINGS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
        session.mkdir(parents=True)
        args = ["tools/fake_stream.py", str(session), "--realtime", "--node", str(spec.node)]
    elif spec.serial_number is None:
        args = ["tools/record_udp.py", name, "--port", str(spec.udp_port)]
    else:
        args = ["tools/record_main.py", spec.serial_number, name]

    recorder = subprocess.Popen(
        PYTHON + args,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # Ctrl+C reaches only this script, which then stops the recorder once
    )

    if spec.fake:
        threading.Thread(target=_copy_lines, args=(recorder.stdout, session / "recorder.log"), daemon=True).start()
        return recorder, session

    deadline = time.time() + RECORDER_START_S

    while True:
        line = recorder.stdout.readline().strip()

        if line.startswith("recording "):
            break

        if recorder.poll() is not None or time.time() > deadline or not line:
            recorder.terminate()
            raise RecorderError(line or f"no 'recording' line in {RECORDER_START_S:.0f} s")

        print(line, file=sys.stderr)  # "port not ready" retries from record_main.py

    session = Path(line.removeprefix("recording "))
    threading.Thread(target=_copy_lines, args=(recorder.stdout, session / "recorder.log"), daemon=True).start()
    return recorder, session


def stop_recorder(recorder: subprocess.Popen) -> None:
    """record_udp/record_main close the gzip chunk on SIGTERM; fake_stream just ends."""
    if recorder.poll() is None:
        recorder.terminate()
        recorder.wait()


# -------------------------------------------------------------------- view

def last_tick(session: Path) -> dict | None:
    path = session / TICKS_FILE

    if not path.exists():
        return None

    with open(path, "rb") as ticks:
        ticks.seek(0, os.SEEK_END)
        ticks.seek(max(0, ticks.tell() - 4096))
        lines = ticks.read().decode("utf-8", errors="replace").strip().splitlines()

    if not lines:
        return None

    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def breathing_view(session: Path, now: float) -> dict:
    """The one line the page shows, from the judge's latest tick."""
    tick = last_tick(session)

    if tick is None:
        return {"label": "판정 시작 중", "colour": "#666", "detail": "첫 창(60초)을 기다립니다"}

    age = now - tick["time"]
    label, colour = BREATHING_STATE.get(tick["state"], (tick["state"], "#333"))
    reason = tick.get("reason") or ""
    progress = re.fullmatch(r"line (\d+/\d+)", reason)

    if not tick.get("input_ok", True):
        label, colour = "입력 끊김", "#333"
    elif age > TICK_STALE_S:
        label, colour = f"{label} (판정 멈춤)", "#333"
    elif tick["state"] == "NO_SIGNAL" and reason.startswith("warming"):
        label = "준비 중"
    elif tick["state"] == "NO_SIGNAL" and progress:
        label = f"확인 중 {progress[1]}"

    rate = "-"

    if tick["state"] == "BREATHING" and tick.get("line") and tick.get("peak_rpm") is not None:
        rate = f"{tick['peak_rpm']:.0f}/분"

    score = tick.get("score")
    threshold = tick.get("threshold")
    score_text = "-" if score is None else f"{score:.1f}"
    threshold_text = "-" if threshold is None else f"{threshold:.1f}"
    detail = f"호흡수 {rate} · 점수 {score_text} / 문턱 {threshold_text} · {age:.0f}초 전"

    return {"label": label, "colour": colour, "detail": detail}


# -------------------------------------------------------------------- page

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>호흡 데모</title>
<style>
  body { font-family: sans-serif; margin: 0; padding: 16px; background: #fafafa; color: #222; }
  h1 { font-size: 1rem; color: #666; margin: 0 0 12px; }
  #state { font-size: 2.6rem; font-weight: bold; padding: 28px 16px; border-radius: 12px;
           color: #fff; background: #666; text-align: center; min-height: 1.2em; }
  #detail { color: #555; margin: 10px 0 20px; text-align: center; min-height: 1.2em; }
  .buttons { display: flex; gap: 10px; }
  button { flex: 1; font-size: 1.3rem; padding: 18px 0; border: 0; border-radius: 10px; color: #fff; }
  button:disabled { opacity: 0.35; }
  #calibrate { background: #06c; } #start { background: #2a2; } #stop { background: #d22; }
  #message { margin-top: 18px; color: #333; min-height: 1.2em; }
  #foot { margin-top: 24px; font-size: 0.85rem; color: #888; }
</style></head>
<body>
<h1>__NAME__ · node __NODE__</h1>
<div id="state">-</div>
<div id="detail"></div>
<div class="buttons">
  <button id="calibrate" type="button">calibrate</button>
  <button id="start" type="button">start</button>
  <button id="stop" type="button">stop</button>
</div>
<div id="message"></div>
<div id="foot"></div>
<script>
const el = id => document.getElementById(id);

function clock(unix) {
  return new Date(unix * 1000).toLocaleTimeString("ko-KR", {hour12: false});
}

function render(s) {
  const state = el("state");
  const detail = el("detail");

  if (s.phase === "calibrating") {
    const left = Math.ceil(s.remaining_s);
    state.textContent = `기준선 측정 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    state.style.background = "#06c";
    detail.textContent = "방을 비우고 인형을 끈 채로 둡니다";
  } else if (s.phase === "running" && s.breathing) {
    state.textContent = s.breathing.label;
    state.style.background = s.breathing.colour;
    detail.textContent = s.breathing.detail;
  } else {
    state.textContent = s.baseline_at ? "대기" : "기준선 없음";
    state.style.background = "#666";
    detail.textContent = s.baseline_at ? `기준선 ${clock(s.baseline_at)}` : "calibrate를 먼저 누릅니다";
  }

  el("calibrate").disabled = s.phase !== "idle";
  el("start").disabled = s.phase !== "idle" || !s.baseline_at;
  el("stop").disabled = s.phase === "idle";
  el("message").textContent = s.message;
  const recording = s.session ? `녹화 ${s.session}` + (s.recorder_alive ? "" : " (녹화 프로세스 없음)") : "녹화 없음";
  el("foot").textContent = recording + " · " + clock(s.now);
}

async function poll() {
  try {
    const r = await fetch("/status.json", {cache: "no-store"});
    render(await r.json());
  } catch (e) {
    el("message").textContent = "서버 연결 끊김";
  }
}

for (const name of ["calibrate", "start", "stop"]) {
  el(name).onclick = async () => {
    el(name).disabled = true;
    const r = await fetch("/" + name, {method: "POST"});
    el("message").textContent = (await r.json()).message;
    poll();
  };
}

poll();
setInterval(poll, 1000);
</script>
</body></html>
"""


def make_handler(demo: Demo):
    page = PAGE.replace("__NAME__", demo.name).replace("__NODE__", str(demo.node)).encode("utf-8")
    actions = {"/calibrate": demo.calibrate, "/start": demo.start, "/stop": demo.stop}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/status.json":
                self._send_json(demo.status())
            else:
                self._send(page, "text/html; charset=utf-8")

        def do_POST(self):
            action = actions.get(self.path)

            if action is None:
                self.send_error(404)
                return

            self._send_json({"message": action()})

        def _send_json(self, value) -> None:
            self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?", default="demo", help="session name (default demo)")
    parser.add_argument("--node", type=int, default=1, help="1 case link (default), 2 Main path")
    parser.add_argument("--minutes", type=float, default=3.0, help="calibration length (default 3)")
    parser.add_argument("--start-delay", type=float, default=0.0,
                        help="seconds between the calibrate button and the baseline start, to leave the room")
    parser.add_argument("--udp-port", type=int, default=3334)
    parser.add_argument("--serial", help="record the main ESP's UART (USB serial number) instead of UDP")
    parser.add_argument("--fake", action="store_true", help="synthetic stream instead of boards (rehearsal)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--baselines-dir", type=Path, default=ROOT / "baselines")
    args = parser.parse_args()

    spec = RecorderSpec(args.udp_port, args.serial, args.fake, args.node)
    demo = Demo(args.name, args.node, args.minutes, args.start_delay, args.baselines_dir, spec, threading.Lock())
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(demo))
    print(f"demo page on http://{lan_address()}:{args.port}/", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        demo.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
