"""Markers of a recording session: `markers.txt`, one line per marker,
`<unix ns>\\t<local ISO time>\\t<text>`. The recorder, `tools/mark.py` and the live display
all write through here, and the replay check reads through here.

The fixed labels below are what the check looks for; `docs/markers.md` documents them.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PAUSE = "멈춤"
RESUME = "재개"
BASELINE_START = "기준선 시작"
BASELINE_END = "기준선 끝"
DOLL_ON = "인형 켬"

MARKER_KEYS = {"p": PAUSE, "r": RESUME, "b": BASELINE_START, "e": BASELINE_END, "d": DOLL_ON}

MARKERS_FILE = "markers.txt"


@dataclass(frozen=True)
class Marker:
    time: float  # unix seconds
    text: str

    def has_label(self, label: str) -> bool:
        """`멈춤` and `멈춤 2` carry the label; `멈춤이 아님` does not."""
        return self.text == label or self.text.startswith(label + " ")


def write_marker(session_dir: Path, text: str, now_ns: int | None = None) -> Marker:
    now_ns = time.time_ns() if now_ns is None else now_ns
    iso_time = datetime.fromtimestamp(now_ns / 1e9).isoformat(timespec="milliseconds")

    with open(session_dir / MARKERS_FILE, "a", encoding="utf-8") as markers:
        markers.write(f"{now_ns}\t{iso_time}\t{text}\n")

    return Marker(time=now_ns / 1e9, text=text)


def read_markers(session_dir: Path) -> list[Marker]:
    path = session_dir / MARKERS_FILE

    if not path.exists():
        return []

    markers = []

    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 2)

        if len(parts) == 3 and parts[0].isdigit():
            markers.append(Marker(time=int(parts[0]) / 1e9, text=parts[2].strip()))

    return markers
