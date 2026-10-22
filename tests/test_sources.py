"""Ticket 01: replay sources yield CsiRecord with aligned time and the DC bin removed."""

import gzip
import struct

import numpy as np

from breathing.sources import (ARI2_HEADER, ARI2_RECORD, CSI_HEADER, FRAME_RECORD, read_ari2_recording,
                               read_frames_recording, synthetic_records)

BINS = 57
HOST_NS = 1_792_114_000_000_000_000


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF

    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF

    return crc


def frame(frame_type: int, payload: bytes) -> bytes:
    body = bytes([frame_type]) + struct.pack("<H", len(payload)) + payload
    return b"\xa5\x5a" + body + struct.pack("<H", crc16_ccitt(body))


def csi_frame(timestamp_us: int, seq: int = 7, rssi: int = -43, agc: int = 33, fft: int = 9, bins: int = BINS,
              iq: bytes | None = None) -> bytes:
    header = CSI_HEADER.pack(1, timestamp_us, seq, rssi, -95, agc, fft, 1, 0, 0, 0, 0, 5, 2 * bins)
    return frame(2, header + (iq if iq is not None else bytes(2 * bins)))


def write_frames(path, frames: list[tuple[int, bytes]]) -> None:
    with gzip.open(path, "wb") as file:
        for host_ns, raw in frames:
            file.write(FRAME_RECORD.pack(host_ns, len(raw)) + raw)


def test_frames_decode_into_aligned_records_without_dc(session_dir):
    iq = bytearray(2 * BINS)
    iq[2 * 27:2 * 27 + 2] = bytes([3, 4])  # bin 27: imag 3, real 4
    iq[2 * 28:2 * 28 + 2] = bytes([0, 0])  # DC, dropped
    iq[2 * 29:2 * 29 + 2] = bytes([0xFF, 2])  # bin 29: imag -1, real 2
    frames = [
        (HOST_NS, frame(1, b"boot text")),
        (HOST_NS, csi_frame(1_000_000, seq=10, iq=bytes(iq))),
        (HOST_NS, csi_frame(1_010_000, seq=11)),
        (HOST_NS, csi_frame(1_020_000, seq=12)[:-1] + b"\x00"),  # bad crc
        (HOST_NS, csi_frame(1_030_000, seq=13, bins=52)),  # not the modal bin count
        (HOST_NS, csi_frame(1_040_000, seq=14)),
    ]
    write_frames(session_dir / "frames.bin.gz", frames)

    records = list(read_frames_recording(session_dir))

    assert [r.seq for r in records] == [10, 11, 14]
    assert records[0].h.shape == (BINS - 1,) and records[0].h.dtype == np.complex64
    assert records[0].h[27] == 4 + 3j and records[0].h[28] == 2 - 1j
    assert records[0].rssi == -43 and records[0].gain == (33, 9)
    host_s = HOST_NS / 1e9
    assert [round(r.time - host_s, 3) for r in records] == [-0.01, 0.0, 0.03]


def test_frames_receiver_clock_wrap_keeps_time_monotone(session_dir):
    wrap = 2**32
    write_frames(session_dir / "frames.bin.gz", [
        (HOST_NS, csi_frame(wrap - 10_000)),
        (HOST_NS, csi_frame(wrap - 1)),
        (HOST_NS + 10_000_000, csi_frame(9_999)),
        (HOST_NS + 20_000_000, csi_frame(19_999)),
    ])

    times = [r.time for r in read_frames_recording(session_dir)]

    assert len(times) == 4
    assert all(b > a for a, b in zip(times, times[1:]))
    assert abs((times[-1] - times[0]) - 0.03) < 1e-6


def ari2_packet(room: int, node: int, timestamp_us: int, seq: int, bins: int = BINS) -> bytes:
    header = ARI2_HEADER.pack(b"ARI2", room, node, 1, 0, 3, seq, seq, timestamp_us, 0, 0, -50, 20, 8, 0, 2 * bins, bins)
    return header + bytes(2 * bins)


def write_chunk(path, packets: list[tuple[int, bytes]], truncate: int = 0) -> None:
    body = b"".join(ARI2_RECORD.pack(host_ns, 0, len(raw)) + raw for host_ns, raw in packets)
    data = gzip.compress(body)
    path.write_bytes(data[:len(data) - truncate] if truncate else data)


def test_ari2_reads_chunks_in_order_filters_stream_and_tolerates_a_cut_chunk(session_dir):
    write_chunk(session_dir / "chunk_0002.ari.gz", [(HOST_NS + 30_000_000, ari2_packet(1, 2, 30_000, 3)),
                                                 (HOST_NS + 40_000_000, ari2_packet(1, 2, 40_000, 4))], truncate=20)
    write_chunk(session_dir / "chunk_0001.ari.gz", [(HOST_NS, ari2_packet(1, 2, 10_000, 1)),
                                                 (HOST_NS, ari2_packet(1, 1, 10_000, 100)),
                                                 (HOST_NS + 10_000_000, ari2_packet(1, 2, 20_000, 2))])

    records = list(read_ari2_recording(session_dir, room=1, node=2))

    assert [r.seq for r in records] == [1, 2, 3]
    assert records[0].h.shape == (BINS - 1,) and records[0].rssi == -50 and records[0].gain == (20, 8)
    assert all(b > a for a, b in zip([r.time for r in records], [r.time for r in records][1:]))


def test_synthetic_records_carry_breathing_on_chosen_bins():
    records = list(synthetic_records(20.0, 15.0, breathing_bins=(3, 9), amplitude=4.0, noise=0.1, seed=2))

    assert len(records) == 2000
    assert records[0].h.shape == (56,)
    amplitude = np.abs(np.stack([r.h for r in records]))
    assert amplitude[:, 3].std() > 2.0 and amplitude[:, 9].std() > 2.0
    assert amplitude[:, 4].std() < 0.5
    assert abs(records[-1].time - records[0].time - 19.99) < 1e-6
