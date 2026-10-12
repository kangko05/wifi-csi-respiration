"""Serial ports -> a queue of (arrival_ns, source index, payload), with stall recovery.

One reader thread per port. A port that gives nothing for STALL_S is closed and reopened
(toggling RTS resets the ESP, which brought the main board back on 2026-10-07 when it
had stopped sending); the stall and the recovery are reported through `log`.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

import serial

from ari2 import Framer

BAUD = 921600
STALL_S = 5.0
REOPEN_WAIT_S = 2.0
READ_CHUNK = 4096


class PortReader(threading.Thread):
    def __init__(self, port: str, source: int, out: "queue.Queue[tuple[int, int, bytes]]",
                 log: Callable[[str], None], baud: int = BAUD, stall_s: float = STALL_S):
        super().__init__(daemon=True, name=f"reader-{source}")
        self.port = port
        self.source = source
        self.out = out
        self.log = log
        self.baud = baud
        self.stall_s = stall_s
        self.framer = Framer()
        self.stop_event = threading.Event()
        self.stalls = 0
        self.last_packet_ns = None

    @property
    def skipped_bytes(self) -> int:
        return self.framer.skipped_bytes

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._read_until_stall()
            except serial.SerialException as error:
                self.log(f"{self.port}: {error}")

            if not self.stop_event.is_set():
                time.sleep(REOPEN_WAIT_S)

    def _read_until_stall(self) -> None:
        with serial.Serial(self.port, self.baud, timeout=0.2) as handle:
            # Opening the port toggles DTR/RTS on most USB bridges and resets the ESP; the
            # boot log that follows is skipped by the framer.
            handle.rts = False
            handle.dtr = False
            self.framer = Framer()
            last_data_s = time.monotonic()
            self.log(f"{self.port}: open")

            while not self.stop_event.is_set():
                data = handle.read(READ_CHUNK)
                now_ns = time.time_ns()

                if data:
                    last_data_s = time.monotonic()

                    for payload in self.framer.feed(data):
                        self.last_packet_ns = now_ns
                        self.out.put((now_ns, self.source, payload))

                elif time.monotonic() - last_data_s > self.stall_s:
                    self.stalls += 1
                    self.log(f"{self.port}: no data for {self.stall_s:.0f} s, reopening (stall {self.stalls})")
                    handle.rts = True
                    time.sleep(0.1)
                    handle.rts = False
                    return

    def stop(self) -> None:
        self.stop_event.set()


def open_readers(ports: list[str], log: Callable[[str], None], baud: int = BAUD):
    """Start one reader per port. Returns (queue, readers)."""
    out: "queue.Queue[tuple[int, int, bytes]]" = queue.Queue()
    readers = []

    for source, port in enumerate(ports):
        reader = PortReader(port, source, out, log, baud)
        reader.start()
        readers.append(reader)

    return out, readers
