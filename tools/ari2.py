"""ARI2 adapter: the ESP boards' packet format -> respiration.Frame.

Packet layout (csi-rx firmware ari2_packet.h, origin/csi-tx): 40 B header + int8 IQ
[imag, real] x bins. The main board writes every room's packets to one USB serial:
node 1 is the in-case link relayed from the room receiver, node 2 the main path (the
room transmitter heard by the main board itself).

This is the only file that knows the format. The package never sees it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from respiration import Frame

HEADER = struct.Struct("<4sBBBBIIIIIIbBbbHH")
MAGIC = b"ARI2"

IN_CASE_NODE = 1
MAIN_PATH_NODE = 2


@dataclass(frozen=True)
class Packet:
    """One parsed packet: where it came from, and the frame the package reads."""

    room: int
    node: int
    rssi: int
    frame: Frame


def packet_length(buffer: bytes, offset: int = 0) -> int | None:
    """Total length of the packet starting at offset, or None when the header is not complete yet."""
    if len(buffer) - offset < HEADER.size:
        return None

    iq_bytes = HEADER.unpack_from(buffer, offset)[-2]

    return HEADER.size + iq_bytes


def parse(payload: bytes, arrival_ns: int) -> Packet | None:
    """One packet's bytes -> Packet, or None when malformed."""
    if len(payload) < HEADER.size or payload[:4] != MAGIC:
        return None

    (_magic, room, node, _channel, _flags, _boot, _packet_seq, _sample_seq, timestamp_us,
     _quarter_dbm, _second, rssi, agc, fft, tx_qdbm, iq_bytes, bins) = HEADER.unpack_from(payload)

    if len(payload) != HEADER.size + iq_bytes or iq_bytes != 2 * bins:
        return None

    iq = np.frombuffer(payload, np.int8, iq_bytes, HEADER.size).reshape(bins, 2).astype(np.int16)
    csi = (iq[:, 1] + 1j * iq[:, 0]).astype(np.complex64)

    frame = Frame(
        t_s=arrival_ns / 1e9,
        device_time_us=timestamp_us,
        csi=csi,
        gain_key=(agc, fft, tx_qdbm),
    )

    return Packet(room=room, node=node, rssi=rssi, frame=frame)


class Framer:
    """Cuts ARI2 packets out of a byte stream (boot log text, partial reads, garbage in between).

    feed(bytes) returns the complete packets found so far. Bytes before a magic word are
    skipped and counted in skipped_bytes (the boot log is a few kB of them).
    """

    def __init__(self):
        self.buffer = bytearray()
        self.skipped_bytes = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer += data
        packets = []

        while True:
            start = self.buffer.find(MAGIC)

            if start < 0:
                # Keep the tail: a magic word may be split across reads.
                keep = len(MAGIC) - 1
                self.skipped_bytes += max(0, len(self.buffer) - keep)
                del self.buffer[:-keep or len(self.buffer)]
                break

            self.skipped_bytes += start
            del self.buffer[:start]

            length = packet_length(self.buffer)

            if length is None or len(self.buffer) < length:
                break

            payload = bytes(self.buffer[:length])

            if parse_ok(payload):
                packets.append(payload)
                del self.buffer[:length]
            else:
                # A magic word inside garbage: skip it and search again.
                self.skipped_bytes += len(MAGIC)
                del self.buffer[:len(MAGIC)]

        return packets


def parse_ok(payload: bytes) -> bool:
    """Cheap sanity check of a candidate packet without building arrays."""
    fields = HEADER.unpack_from(payload)
    iq_bytes = fields[-2]
    bins = fields[-1]

    return iq_bytes == 2 * bins and 0 < bins <= 256
