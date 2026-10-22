"""ARI2 packets as the main unit (csi-main, relay mode) streams them over its UART.

One packet = 40-byte header + int8 IQ pairs, little-endian:
  "<4sBBBBIIIIIIbBbbHH" = magic room node channel flags boot_id packet_seq sample_seq
  timestamp_us unused unused rssi agc fft tx_power_qdbm iq_bytes bins
Recording files are `.ari.gz` chunks with records "<QIH" (host unix ns, source IPv4 or 0,
packet length) + packet, the layout the 10-13 and 10-14 night recordings use, so the same
reader (`B-scripts/ari2_grid.py`, `features.py`) serves both.
"""

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct("<4sBBBBIIIIIIbBbbHH")
RECORD = struct.Struct("<QIH")
MAGIC = b"ARI2"
HT20_PACKET_BYTES = 154


@dataclass
class Ari2Packet:
    room: int
    node: int
    boot_id: int
    packet_seq: int
    timestamp_us: int
    rssi: int
    agc: int
    fft: int
    bins: int
    raw: bytes

    @property
    def stream(self) -> str:
        return f"room{self.room}_node{self.node}"


def parse_packet(raw: bytes) -> Ari2Packet | None:
    """None when the bytes are not one whole, self-consistent packet."""
    if len(raw) < HEADER.size or raw[:4] != MAGIC:
        return None

    fields = HEADER.unpack_from(raw)
    iq_bytes = fields[15]
    bins = fields[16]

    if iq_bytes != 2 * bins or len(raw) != HEADER.size + iq_bytes:
        return None

    return Ari2Packet(room=fields[1], node=fields[2], boot_id=fields[5], packet_seq=fields[6],
                      timestamp_us=fields[8], rssi=fields[11], agc=fields[12], fft=fields[13],
                      bins=bins, raw=raw)


class Ari2Decoder:
    """Turns the UART byte stream into packets; bytes between packets are kept as text
    (boot log before the relay starts, nothing afterwards)."""

    def __init__(self, packet_bytes: int = HT20_PACKET_BYTES) -> None:
        self.packet_bytes = packet_bytes
        self._buffer = bytearray()
        self.free_text = bytearray()
        self.bad_packets = 0

    def feed(self, data: bytes) -> list[Ari2Packet]:
        self._buffer.extend(data)
        packets = []

        while True:
            start = self._buffer.find(MAGIC)

            if start < 0:
                keep = len(self._buffer) if self._buffer.endswith(MAGIC[:3]) else 0
                self._skip(len(self._buffer) - keep)
                return packets

            if start > 0:
                self._skip(start)

            if len(self._buffer) < self.packet_bytes:
                return packets

            packet = parse_packet(bytes(self._buffer[:self.packet_bytes]))

            if packet is None:
                self.bad_packets += 1
                self._skip(1)
                continue

            packets.append(packet)
            del self._buffer[:self.packet_bytes]

    def _skip(self, count: int) -> None:
        if count <= 0:
            return

        self.free_text.extend(self._buffer[:count])
        del self._buffer[:count]

    def take_free_text(self) -> str:
        text = self.free_text.decode("utf-8", errors="replace")
        self.free_text.clear()
        return text


class ChunkWriter:
    """Writes `.ari.gz` chunk files of fixed wall-clock length into one session folder."""

    def __init__(self, session_dir: Path, chunk_s: float, name_at) -> None:
        """`name_at(host_ns)` gives the file stem for the chunk that starts at host_ns."""
        self.session_dir = session_dir
        self.chunk_s = chunk_s
        self.name_at = name_at
        self._file = None
        self._chunk_start_ns = None

    def write(self, host_ns: int, packet: Ari2Packet) -> None:
        if self._file is None or host_ns - self._chunk_start_ns >= self.chunk_s * 1e9:
            self._open(host_ns)

        self._file.write(RECORD.pack(host_ns, 0, len(packet.raw)))
        self._file.write(packet.raw)

    def _open(self, host_ns: int) -> None:
        self.close()
        self._chunk_start_ns = host_ns
        self._file = gzip.open(self.session_dir / f"{self.name_at(host_ns)}.ari.gz", "wb", compresslevel=4)

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
