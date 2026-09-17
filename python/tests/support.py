"""Fakes for offline tests: scripted serial stream and a controllable clock."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

EPOCH = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Monotonic clock advanced explicitly by the fake stream."""

    def __init__(self, start: float = 0.0, idle_step: float = 0.05) -> None:
        self.t = start
        self.idle_step = idle_step

    def monotonic(self) -> float:
        return self.t

    def utcnow(self) -> datetime:
        return EPOCH + timedelta(seconds=self.t)

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeSerial:
    """Replays scripted events; empty reads advance the clock like a timeout would.

    Each event is `(release_time, payload)` where payload is bytes to deliver or
    an exception instance to raise once the clock reaches `release_time`.
    """

    def __init__(self, clock: FakeClock, events, chunk_size: int | None = None) -> None:
        self.clock = clock
        self.events = list(events)
        self.chunk_size = chunk_size
        self.pending = bytearray()
        self.closed = False
        self.written = bytearray()

    def _release(self) -> None:
        while self.events and self.events[0][0] <= self.clock.t:
            _, payload = self.events.pop(0)
            if isinstance(payload, BaseException):
                raise payload
            self.pending.extend(payload)

    @property
    def in_waiting(self) -> int:
        self._release()
        return len(self.pending)

    def read(self, size: int) -> bytes:
        self._release()
        if not self.pending:
            self.clock.advance(self.clock.idle_step)
            return b""
        limit = size if self.chunk_size is None else min(size, self.chunk_size)
        data = bytes(self.pending[:limit])
        del self.pending[:limit]
        self.clock.advance(0.001)
        return data

    def write(self, data: bytes) -> int:  # pragma: no cover - must never be called
        self.written.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class RecordingSerial(FakeSerial):
    """Serial double that records the configuration calls the collector makes."""

    ops: list

    def __init__(self, ops, clock, events, **kwargs) -> None:
        super().__init__(clock, events)
        self.ops = ops
        self._port = kwargs.get("port")
        self.baudrate = kwargs.get("baudrate")
        self.timeout = kwargs.get("timeout")
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.is_open = False
        ops.append(("construct", kwargs.get("port"), kwargs.get("baudrate"), kwargs.get("timeout")))

    @property
    def port(self):
        return self._port

    @port.setter
    def port(self, value):
        self._port = value
        self.ops.append(("set_port", value))

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value
        self.ops.append(("set_dtr", value))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value
        self.ops.append(("set_rts", value))

    def open(self) -> None:
        self.is_open = True
        self.ops.append(("open", self._port))

    def reset_input_buffer(self) -> None:  # pragma: no cover - must never be called
        self.ops.append(("reset_input_buffer",))

    def close(self) -> None:
        super().close()
        self.ops.append(("close",))


def csi_line(
    payload: bytes,
    *,
    seq: int = 1,
    local_timestamp: int = 123456789,
    first_word: int = 0,
    declared_len: int | None = None,
    data_text: str | None = None,
) -> bytes:
    """Build one firmware-shaped CSI_DATA line (CRLF terminated, as the RX prints)."""
    body = data_text if data_text is not None else base64.b64encode(payload).decode("ascii")
    fields = [
        "CSI_DATA",
        str(seq),
        "-41",
        "-92",
        "0",
        "36",
        "36",
        str(local_timestamp),
        "119",
        "1",
        str(len(payload) if declared_len is None else declared_len),
        str(first_word),
        "1.000000",
        "0",
        body,
    ]
    return (",".join(fields)).encode("ascii") + b"\r\n"


HEADER_LINE = (
    b"type,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,"
    b"sig_len,rx_format,len,first_word,compensate_gain,dropped,data\r\n"
)
LOG_LINE = b"I (312) csi_rx: ================ CSI RECV ================\r\n"
