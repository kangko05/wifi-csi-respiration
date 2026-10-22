"""The Presence judge on a session the recorder is still writing.

Six streams are tailed on their own threads; each thread turns its records into
StreamWindows. The main loop judges a step once every stream has had `grace_s` to deliver its
window for that step, so one slow stream delays the tick by that much and a missing stream
counts as no window. Each tick is appended to `presence_ticks.jsonl` in the session.

Thresholds come from a JSON file written by `python -m presence thresholds`, because the
empty-home interval that sets them was recorded hours before the live loop starts.
"""

import json
import queue
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from breathing.scoring import QuietBaseline
from breathing.sources import tail_ari2_recording
from presence.features import StreamWindow, stream_windows
from presence.judge import PresenceConfig, PresenceJudge, RoomThresholds, RoomWindow

TICKS_FILE = "presence_ticks.jsonl"


def save_thresholds(thresholds: Mapping[int, RoomThresholds], path: Path) -> None:
    data = {str(room): {"motion": json.loads(t.motion.to_json()), "line": json.loads(t.line.to_json()),
                        **({} if t.main_motion is None else {"main_motion": json.loads(t.main_motion.to_json())})}
            for room, t in thresholds.items()}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_thresholds(path: Path) -> dict[int, RoomThresholds]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(room): RoomThresholds(motion=QuietBaseline.from_json(json.dumps(t["motion"])),
                                      line=QuietBaseline.from_json(json.dumps(t["line"])),
                                      main_motion=None if "main_motion" not in t
                                      else QuietBaseline.from_json(json.dumps(t["main_motion"])))
            for room, t in data.items()}


def run_live(session_dir: Path, config: PresenceConfig, thresholds: Mapping[int, RoomThresholds],
             grace_s: float = 5.0) -> None:
    """Never returns. Starts from one window back so the first tick lands within a minute."""
    windows: queue.Queue[tuple[int, int, StreamWindow]] = queue.Queue()
    since = time.time() - config.breathing.window_s

    for room in config.rooms:
        for node in (config.case_node, config.main_node):
            threading.Thread(target=_tail, args=(session_dir, room, node, config, since, windows), daemon=True).start()

    judge = PresenceJudge(config, thresholds)
    pending: dict[float, dict[tuple[int, int], StreamWindow]] = {}
    judged_until = 0.0

    with open(session_dir / TICKS_FILE, "a", encoding="utf-8") as log:
        while True:
            try:
                room, node, window = windows.get(timeout=1.0)
                if window.end > judged_until:
                    pending.setdefault(window.end, {})[room, node] = window
            except queue.Empty:
                pass

            now = time.time()

            for end in sorted(e for e in pending if e + grace_s <= now):
                by_stream = pending.pop(end)
                room_windows = {room: RoomWindow(case=by_stream.get((room, config.case_node)),
                                                 main=by_stream.get((room, config.main_node)))
                                for room in config.rooms}
                log.write(judge.step(end, room_windows).to_json() + "\n")
                log.flush()
                judged_until = end


def _tail(session_dir: Path, room: int, node: int, config: PresenceConfig, since: float, out: queue.Queue) -> None:
    records = (r for r in tail_ari2_recording(session_dir, room, node, since=since) if r is not None)

    for window in stream_windows(records, config.breathing, config.motion_rate_hz, config.where_window_s):
        out.put((room, node, window))
