"""Input sources of the Breathing module.

Every source yields `CsiRecord`: the csi-link `frames.bin.gz` replay, the csi-main ARI2
`.ari.gz` replay, the synthetic stream for tests, and the two live sources. A live source
(`tail_ari2_recording`, `live_frames`) also yields `None` as a heartbeat whenever a poll
brought no record, so the consumer can notice input loss. The judge never sees frames,
packets or clocks.

Record time is unix seconds on the host wall clock. Host timestamps are batched (one
serial read stamps about ten packets), so the receiver's microsecond clock is unwrapped
and anchored to the host clock instead. The csi-link replay anchors with the median
offset of the whole file; the ARI2 reader and the live sources anchor on the first record
of the stream and renew the anchor when the offset jumps by more than `REANCHOR_S`
(a board reboot restarts the receiver clock).
"""

import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CsiRecord:
    time: float  # unix seconds, aligned to the host wall clock
    h: np.ndarray  # complex64, driver order, DC bin removed
    seq: int
    rssi: int
    gain: tuple[int, int]  # (agc, fft)


FRAME_SYNC = b"\xa5\x5a"
FRAME_TYPE_CSI = 2
FRAME_RECORD = struct.Struct("<QH")  # host_ns, frame length
FRAME_PREFIX = struct.Struct("<2sBH")  # sync, type, payload length
FRAME_CRC = struct.Struct("<H")
CSI_HEADER = struct.Struct("<IIHbbBbBBBBBBH")

ARI2_MAGIC = b"ARI2"
ARI2_RECORD = struct.Struct("<QIH")  # host_ns, source ip, packet length
ARI2_HEADER = struct.Struct("<4sBBBBIIIIIIbBbbHH")

CLOCK_WRAP_US = 2**32
REANCHOR_S = 1.0  # a host-minus-receiver offset jump beyond this is a receiver restart, not jitter
CHUNK_GLOB = "chunk_*.ari.gz"


def read_frames_recording(session_dir: Path) -> Iterator[CsiRecord]:
    """csi-link session: CSI frames of `frames.bin.gz`, modal bin count only."""
    records = list(_GzipRecords(session_dir / "frames.bin.gz", FRAME_RECORD).read())
    host_ns = np.array([host for host, _ in records], dtype=np.int64)
    frames = [frame for _, frame in records]
    valid = _frames_with_valid_crc(frames)
    keep = [i for i, frame in enumerate(frames) if valid[i] and frame[2] == FRAME_TYPE_CSI]
    payloads = [frames[i][FRAME_PREFIX.size:-FRAME_CRC.size] for i in keep]

    headers = np.array([CSI_HEADER.unpack_from(p) for p in payloads], dtype=np.int64).reshape(-1, 14)
    csi_length = headers[:, 13]
    iq = [p[CSI_HEADER.size:CSI_HEADER.size + n] for p, n in zip(payloads, csi_length)]

    yield from _records(host_ns[keep], timestamp_us=headers[:, 1], seq=headers[:, 2], rssi=headers[:, 3],
                        agc=headers[:, 5], fft=headers[:, 6], iq=iq)


def read_ari2_recording(session_dir: Path, room: int, node: int) -> Iterator[CsiRecord]:
    """csi-main session: every `chunk_*.ari.gz` in name order, one (room, node) stream."""
    stream = Ari2Stream(room, node)

    for chunk in sorted(session_dir.glob(CHUNK_GLOB)):
        for host_ns, packet in _GzipRecords(chunk, ARI2_RECORD).read():
            record = stream.decode(host_ns, packet)

            if record is not None:
                yield record


def tail_ari2_recording(session_dir: Path, room: int, node: int, poll_s: float = 0.5,
                        since: float | None = None) -> Iterator[CsiRecord | None]:
    """Follow a session that is still being recorded: the newest chunk as it is flushed, then
    each newer chunk as it appears. Records stamped before `since` (unix seconds) are
    skipped. Yields `None` after every poll that brought no record. Never returns."""
    stream = Ari2Stream(room, node)
    since_ns = None if since is None else int(since * 1e9)
    chunk: _GzipRecords | None = None

    while True:
        newest = max(session_dir.glob(CHUNK_GLOB), default=None, key=lambda p: p.name)
        produced = 0

        if chunk is not None:
            for host_ns, packet in chunk.read():
                record = stream.decode(host_ns, packet)

                if record is not None and (since_ns is None or host_ns >= since_ns):
                    produced += 1
                    yield record

        # The writer closes a chunk before it creates the next one, so a newer file listed
        # before the read above means the current chunk was read to its end.
        if newest is not None and (chunk is None or newest.name > chunk.path.name):
            chunk = _GzipRecords(newest, ARI2_RECORD)
            continue

        if produced == 0:
            time.sleep(poll_s)
            yield None


def live_frames(connection) -> Iterator[CsiRecord | None]:
    """csi-link RX on a serial port: `connection.read(n)` returns the bytes that arrived
    within its timeout. Yields `None` after every read that completed no CSI frame."""
    stream = FrameStream()

    while True:
        data = connection.read(65536)
        records = stream.feed(time.time_ns(), data) if data else []

        if not records:
            yield None

        yield from records


def synthetic_records(duration_s: float, breaths_per_min: float | None, *, rate_hz: float = 100.0,
                      pauses: tuple[tuple[float, float], ...] = (), amplitude: float = 3.0,
                      noise: float = 1.0, level: float = 20.0, n_bins: int = 56,
                      breathing_bins: tuple[int, ...] = tuple(range(2, 56, 5)), start: float = 0.0,
                      seed: int = 0) -> Iterator[CsiRecord]:
    """A stream at `rate_hz` whose amplitude on `breathing_bins` carries a sinusoid at
    `breaths_per_min` (None: noise only). Inside `pauses` (offsets from `start`, seconds)
    the chest is still, so those bins hold their resting amplitude."""
    rng = np.random.default_rng(seed)
    count = int(round(duration_s * rate_hz))
    phase = np.exp(1j * rng.uniform(0.0, 2.0 * np.pi, n_bins))
    bins = list(breathing_bins)

    for i in range(count):
        t = i / rate_hz
        paused = any(a <= t < b for a, b in pauses)
        amp = np.full(n_bins, level, dtype=np.float64)

        if breaths_per_min is not None and not paused:
            amp[bins] += amplitude * np.sin(2.0 * np.pi * breaths_per_min / 60.0 * t)

        h = amp * phase + noise * (rng.standard_normal(n_bins) + 1j * rng.standard_normal(n_bins))
        yield CsiRecord(time=start + t, h=h.astype(np.complex64), seq=i, rssi=-50, gain=(1, 0))


class _GzipRecords:
    """Length-prefixed records of one gzip file, read incrementally: each `read` inflates
    the bytes appended since the previous call and yields the records they complete. A
    record cut by a flush or by the file's end waits for the rest; a file that ends
    mid-record (power loss, chunk still being written) yields what was complete."""

    def __init__(self, path: Path, record: struct.Struct) -> None:
        self.path = path
        self._record = record
        self._offset = 0
        self._inflater = zlib.decompressobj(wbits=31)
        self._pending = bytearray()

    def read(self) -> Iterator[tuple[int, bytes]]:
        with open(self.path, "rb") as file:
            file.seek(self._offset)
            data = file.read()

        self._offset += len(data)

        if data and not self._inflater.eof:
            self._pending += self._inflater.decompress(data)

        header_size = self._record.size
        position = 0

        try:
            while len(self._pending) - position >= header_size:
                fields = self._record.unpack_from(self._pending, position)
                length = fields[-1]

                if len(self._pending) - position < header_size + length:
                    return

                yield fields[0], bytes(self._pending[position + header_size:position + header_size + length])
                position += header_size + length
        finally:
            del self._pending[:position]


class _ReceiverClock:
    """Receiver microseconds to host seconds, one record at a time."""

    def __init__(self) -> None:
        self._last_us: int | None = None
        self._wraps = 0
        self._offset: float | None = None

    def align(self, host_ns: int, timestamp_us: int) -> float:
        if self._last_us is not None and timestamp_us < self._last_us:
            self._wraps += 1

        self._last_us = timestamp_us
        receiver_s = (timestamp_us + self._wraps * CLOCK_WRAP_US) / 1e6
        offset = host_ns / 1e9 - receiver_s

        if self._offset is None or abs(offset - self._offset) > REANCHOR_S:
            self._offset = offset

        return receiver_s + self._offset


class Ari2Stream:
    """ARI2 packets of one (room, node) in arrival order to records. The bin count of the
    first packet is kept; packets with another count are dropped."""

    def __init__(self, room: int, node: int) -> None:
        self._room = room
        self._node = node
        self._clock = _ReceiverClock()
        self._bins: int | None = None

    def decode(self, host_ns: int, packet: bytes) -> CsiRecord | None:
        if len(packet) < ARI2_HEADER.size or packet[:4] != ARI2_MAGIC:
            return None

        if packet[4] != self._room or packet[5] != self._node:
            return None

        fields = ARI2_HEADER.unpack_from(packet)
        iq = packet[ARI2_HEADER.size:ARI2_HEADER.size + fields[15]]
        bins = len(iq) // 2

        if self._bins is None and bins > 0:
            self._bins = bins

        if bins != self._bins:
            return None

        return CsiRecord(time=self._clock.align(host_ns, fields[8]), h=_h_from_iq(iq, bins // 2),
                         seq=fields[6], rssi=fields[11], gain=(fields[12], fields[13]))


class FrameStream:
    """csi-link UART bytes to records: frames `A5 5A | type | len | payload | crc16`,
    CRC checked per frame, text frames and bad frames dropped."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._clock = _ReceiverClock()
        self._bins: int | None = None

    def feed(self, host_ns: int, data: bytes) -> list[CsiRecord]:
        self._buffer += data
        records = []

        while True:
            start = self._buffer.find(FRAME_SYNC)

            if start < 0:
                keep = 1 if self._buffer.endswith(FRAME_SYNC[:1]) else 0
                del self._buffer[:len(self._buffer) - keep]
                return records

            del self._buffer[:start]

            if len(self._buffer) < FRAME_PREFIX.size:
                return records

            _, frame_type, length = FRAME_PREFIX.unpack_from(self._buffer)
            total = FRAME_PREFIX.size + length + FRAME_CRC.size

            if len(self._buffer) < total:
                return records

            frame = bytes(self._buffer[:total])

            if _crc16(frame[2:-FRAME_CRC.size]) != FRAME_CRC.unpack_from(frame, total - FRAME_CRC.size)[0]:
                del self._buffer[:len(FRAME_SYNC)]
                continue

            del self._buffer[:total]

            if frame_type == FRAME_TYPE_CSI:
                record = self._decode(host_ns, frame[FRAME_PREFIX.size:-FRAME_CRC.size])

                if record is not None:
                    records.append(record)

    def _decode(self, host_ns: int, payload: bytes) -> CsiRecord | None:
        if len(payload) < CSI_HEADER.size:
            return None

        header = CSI_HEADER.unpack_from(payload)
        iq = payload[CSI_HEADER.size:CSI_HEADER.size + header[13]]
        bins = len(iq) // 2

        if self._bins is None and bins > 0:
            self._bins = bins

        if bins != self._bins:
            return None

        return CsiRecord(time=self._clock.align(host_ns, header[1]), h=_h_from_iq(iq, bins // 2),
                         seq=header[2], rssi=header[3], gain=(header[5], header[6]))


def _crc16_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.uint16)

    for byte in range(256):
        crc = byte << 8

        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF

        table[byte] = crc

    return table


CRC16_TABLE = _crc16_table()


def _crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE of one frame body (type, length, payload)."""
    crc = 0xFFFF

    for byte in data:
        crc = ((crc << 8) ^ int(CRC16_TABLE[(crc >> 8) ^ byte])) & 0xFFFF

    return crc


def _frames_with_valid_crc(frames: list[bytes]) -> np.ndarray:
    """The same CRC over every frame of a replay, run per frame-length group so numpy walks
    the bytes of every frame of that length at once."""
    valid = np.zeros(len(frames), dtype=bool)
    lengths = np.array([len(f) for f in frames], dtype=np.int64)

    for length in np.unique(lengths):
        if length < 7:
            continue

        index = np.flatnonzero(lengths == length)
        block = np.frombuffer(b"".join(frames[i] for i in index), dtype=np.uint8).reshape(len(index), length)
        crc = np.full(len(index), 0xFFFF, dtype=np.uint16)

        for column in range(2, length - 2):
            crc = (crc << 8) ^ CRC16_TABLE[(crc >> 8) ^ block[:, column]]

        expected = block[:, -2].astype(np.uint16) | (block[:, -1].astype(np.uint16) << 8)
        valid[index] = (block[:, 0] == 0xA5) & (block[:, 1] == 0x5A) & (crc == expected)

    return valid


def _h_from_iq(iq: bytes, dc: int) -> np.ndarray:
    """int8 (imag, real) pairs in driver order with the DC bin at `dc` removed."""
    pairs = np.frombuffer(iq, dtype=np.int8).reshape(-1, 2).astype(np.float32)
    return np.delete(pairs[:, 1] + 1j * pairs[:, 0], dc).astype(np.complex64)


def _records(host_ns, *, timestamp_us, seq, rssi, agc, fft, iq: list[bytes]) -> Iterator[CsiRecord]:
    if len(iq) == 0:
        return

    bin_count = np.array([len(b) // 2 for b in iq])
    modal = int(np.bincount(bin_count).argmax())
    keep = np.flatnonzero(bin_count == modal)
    dc = modal // 2  # the driver's HT20 layout puts DC in the middle (bin 28 of 57)

    wraps = np.cumsum(np.r_[0, np.diff(timestamp_us[keep]) < 0])
    receiver_s = (timestamp_us[keep] + wraps * CLOCK_WRAP_US) / 1e6
    offset = float(np.median(host_ns[keep] / 1e9 - receiver_s))
    time_s = receiver_s + offset

    for n, i in enumerate(keep):
        yield CsiRecord(time=float(time_s[n]), h=_h_from_iq(iq[i], dc), seq=int(seq[i]), rssi=int(rssi[i]),
                        gain=(int(agc[i]), int(fft[i])))
