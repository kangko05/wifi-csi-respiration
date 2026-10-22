"""Recording files of one board session.

A session folder holds:
  frames.bin.gz   records of <Q host_ns> <H frame_length> <frame bytes>; the frame bytes
                  include magic, header and crc, so the file re-decodes exactly as received
  markers.txt     "<host_ns>\\t<iso time>\\t<label>" lines typed during the session
  session.txt     port, start time and the board config text at start

Same layout as the csi-test recordings on ksrv, so one reader serves both.
"""

import gzip
import struct
from collections.abc import Iterator
from pathlib import Path

from cli.frames import Frame, FrameDecoder

RECORD_HEADER = struct.Struct("<QH")


class FrameWriter:
    def __init__(self, path: Path) -> None:
        self._file = gzip.open(path, "wb", compresslevel=4)

    def write(self, host_ns: int, frame: Frame) -> None:
        self._file.write(RECORD_HEADER.pack(host_ns, len(frame.raw)))
        self._file.write(frame.raw)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def read_frames(path: Path) -> Iterator[tuple[int, Frame]]:
    """Yields (host_ns, frame). Tolerates a file cut off mid-record, e.g. after a power loss."""
    decoder = FrameDecoder()

    with gzip.open(path, "rb") as file:
        while True:
            try:
                header = file.read(RECORD_HEADER.size)
            except EOFError:
                return

            if len(header) < RECORD_HEADER.size:
                return

            host_ns, length = RECORD_HEADER.unpack(header)

            try:
                raw = file.read(length)
            except EOFError:
                return

            if len(raw) < length:
                return

            for frame in decoder.feed(raw):
                yield host_ns, frame
