"""The collector's parts: ARI2 framing and parsing, chunk recording, the hold score."""

import numpy as np

from ari2 import HEADER, MAGIC, Framer, parse
from holds import HoldSchedule, score
from recorder import ChunkRecorder, read_records


def packet_bytes(room=1, node=2, bins=57, seq=0, timestamp_us=123456) -> bytes:
    iq = np.zeros((bins, 2), dtype=np.int8)
    iq[:, 0] = 3   # imag
    iq[:, 1] = 4   # real
    iq[bins // 2] = 0

    head = HEADER.pack(MAGIC, room, node, 157, 0, 1, seq, seq, timestamp_us, 0, 0, -45, 30, 4, 8, 2 * bins, bins)

    return head + iq.tobytes()


def test_parse_gives_a_frame_with_complex_csi_and_gain_key():
    packet = parse(packet_bytes(), arrival_ns=2_000_000_000)

    assert (packet.room, packet.node, packet.rssi) == (1, 2, -45)
    assert packet.frame.t_s == 2.0
    assert packet.frame.device_time_us == 123456
    assert packet.frame.gain_key == (30, 4, 8)
    assert packet.frame.csi.shape == (57,)
    assert packet.frame.csi[0] == 4 + 3j
    assert packet.frame.csi[28] == 0


def test_framer_skips_boot_log_and_joins_split_reads():
    framer = Framer()
    boot = b"I (123) wifi: ari2 stream: room=1 node=1 bins=57\r\n"
    first = packet_bytes(seq=1)
    second = packet_bytes(seq=2)
    stream = boot + first + second

    found = []
    for cut in range(0, len(stream), 37):
        found += framer.feed(stream[cut:cut + 37])

    assert found == [first, second]
    assert framer.skipped_bytes == len(boot)


def test_framer_recovers_from_a_magic_word_inside_garbage():
    framer = Framer()
    garbage = MAGIC + b"\xff" * 10
    packet = packet_bytes(seq=5)

    found = framer.feed(garbage + packet)

    assert found == [packet]


def test_parse_rejects_wrong_lengths():
    assert parse(packet_bytes()[:-1], 0) is None
    assert parse(b"\x00" * 200, 0) is None


def test_chunk_recorder_round_trip(tmp_path):
    recorder = ChunkRecorder(tmp_path, chunk_s=1.0)
    payloads = [packet_bytes(seq=i) for i in range(3)]

    recorder.write(10_000_000_000, 0, payloads[0])
    recorder.write(10_500_000_000, 0, payloads[1])
    recorder.write(12_000_000_000, 1, payloads[2])   # 2 s later: a new chunk
    recorder.close()

    assert len(recorder.files) == 2

    records = []
    for path in recorder.files:
        records += list(read_records(path))

    assert [r[2] for r in records] == payloads
    assert [r[1] for r in records] == [0, 0, 1]


def test_hold_schedule_and_score():
    schedule = HoldSchedule(first_hold_s=100, hold_s=20, period_s=120, count=3)
    holds = schedule.holds()

    assert holds == [(100, 120), (220, 240), (340, 360)]

    # Hits 12 s into holds 1 and 3, a false alarm at 180 s, hold 2 failed (its start is ignored)
    result = score([112.0, 180.0, 232.0, 352.0], holds, failed={1})

    assert result["valid_holds"] == 2
    assert result["hits"] == 2
    assert result["delays_s"] == [12.0, 12.0]
    assert result["missed"] == []
    assert result["false_alarms"] == [180.0]
