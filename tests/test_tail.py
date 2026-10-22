"""Ticket 04: the live tail follows a session the recorder is still writing."""

import gzip

from breathing.sources import ARI2_RECORD, tail_ari2_recording
from test_sources import HOST_NS, ari2_packet

POLL_S = 0.05


def record_bytes(host_ns: int, packet: bytes) -> bytes:
    return ARI2_RECORD.pack(host_ns, 0, len(packet)) + packet


def test_tail_yields_flushed_records_heartbeats_while_idle_and_rolls_over(session_dir):
    tail = tail_ari2_recording(session_dir, room=1, node=2, poll_s=POLL_S)

    assert next(tail) is None, "no chunk yet is a heartbeat, not an error"

    chunk1 = gzip.open(session_dir / "chunk_0001.ari.gz", "wb")
    chunk1.write(record_bytes(HOST_NS, ari2_packet(1, 2, 10_000, 1)))
    chunk1.write(record_bytes(HOST_NS, ari2_packet(1, 1, 10_000, 100)))
    second = record_bytes(HOST_NS + 10_000_000, ari2_packet(1, 2, 20_000, 2))
    chunk1.write(second[:50])
    chunk1.flush()

    assert next(tail).seq == 1
    assert next(tail) is None, "a record cut by the flush waits for its rest"

    chunk1.write(second[50:])
    chunk1.flush()

    assert next(tail).seq == 2

    chunk1.close()
    chunk2 = gzip.open(session_dir / "chunk_0002.ari.gz", "wb")
    chunk2.write(record_bytes(HOST_NS + 20_000_000, ari2_packet(1, 2, 30_000, 3)))
    chunk2.flush()

    third = next(tail)
    assert third.seq == 3 and third.h.shape == (56,)
    assert next(tail) is None

    chunk2.close()


def test_tail_skips_records_before_since(session_dir):
    with gzip.open(session_dir / "chunk_0001.ari.gz", "wb") as chunk:
        chunk.write(record_bytes(HOST_NS, ari2_packet(1, 2, 10_000, 1)))
        chunk.write(record_bytes(HOST_NS + 2_000_000_000, ari2_packet(1, 2, 2_010_000, 2)))

    tail = tail_ari2_recording(session_dir, room=1, node=2, poll_s=POLL_S, since=HOST_NS / 1e9 + 1.0)

    assert next(tail).seq == 2
