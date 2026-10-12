"""Recording: raw packets with their arrival time, in 10 minute gzip chunks; markers; schedules.

File format (same as the earlier live recordings, so old tools read new files):
    repeated [8 B arrival unix_ns][4 B source id][2 B length][payload]
A chunk is closed and a new one started every CHUNK_S, so a crash loses at most that much.
"""

from __future__ import annotations

import gzip
import struct
import time
from datetime import datetime
from pathlib import Path

RECORD = struct.Struct("<QIH")
CHUNK_S = 600.0


def chunk_name(prefix: str, unix_ns: int) -> str:
    stamp = datetime.fromtimestamp(unix_ns / 1e9).strftime("%Y%m%d_%H%M%S")

    return f"{prefix}_{stamp}.ari.gz"


class ChunkRecorder:
    """Writes (arrival_ns, source, payload) records, rotating the file every chunk_s."""

    def __init__(self, folder: Path, prefix: str = "live", chunk_s: float = CHUNK_S):
        self.folder = Path(folder)
        self.prefix = prefix
        self.chunk_s = chunk_s
        self.stream = None
        self.chunk_start_ns = None
        self.files: list[Path] = []

        self.folder.mkdir(parents=True, exist_ok=True)

    def write(self, arrival_ns: int, source: int, payload: bytes) -> None:
        if self.stream is None or arrival_ns - self.chunk_start_ns >= self.chunk_s * 1e9:
            self._rotate(arrival_ns)

        self.stream.write(RECORD.pack(arrival_ns, source, len(payload)))
        self.stream.write(payload)

    def _rotate(self, arrival_ns: int) -> None:
        self.close()

        path = self.folder / chunk_name(self.prefix, arrival_ns)
        self.stream = gzip.open(path, "wb")
        self.chunk_start_ns = arrival_ns
        self.files.append(path)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def read_records(path: Path):
    """Records of one chunk as (arrival_ns, source, payload); stops at a truncated tail."""
    with gzip.open(path, "rb") as stream:
        while True:
            try:
                head = stream.read(RECORD.size)
            except (EOFError, OSError):
                return

            if len(head) < RECORD.size:
                return

            arrival_ns, source, length = RECORD.unpack(head)

            try:
                payload = stream.read(length)
            except (EOFError, OSError):
                return

            if len(payload) < length:
                return

            yield arrival_ns, source, payload


class TextLog:
    """Timestamped lines (markers, events, schedule rows) appended to a text file, flushed at once."""

    def __init__(self, path: Path, header: str | None = None):
        self.path = Path(path)
        is_new = not self.path.exists()
        self.handle = open(self.path, "a", encoding="utf-8")

        if is_new and header is not None:
            self.handle.write(header + "\n")
            self.handle.flush()

    def write(self, text: str, when_s: float | None = None) -> None:
        if when_s is None:
            when_s = time.time()

        stamp = datetime.fromtimestamp(when_s).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.handle.write(f"{stamp}\t{when_s:.3f}\t{text}\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()
