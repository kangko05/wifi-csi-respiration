"""Host check of the firmware's ARI2 packing against the PC parser (report 0014).

Compiles the real firmware source ``csi-rx/main/ari2_packet.c`` with the host
C compiler together with ``ari2_pack_dump.c``, runs it, and parses the packets
with ``csi_presence.ari2.parse_packet`` of the PC repository (read only).

Run with the PC project's interpreter (it has numpy). PYTHONDONTWRITEBYTECODE
keeps __pycache__ out of the read-only PC repository:

    PYTHONDONTWRITEBYTECODE=1 \\
    /home/kang/.workspace/csi-occupancy-breathing/.venv/bin/python -m pytest \\
        -p no:cacheprovider host/tests/test_ari2_packet.py
    # or, without pytest:
    PYTHONDONTWRITEBYTECODE=1 /home/kang/.workspace/csi-occupancy-breathing/.venv/bin/python \\
        host/tests/test_ari2_packet.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_MAIN = REPO_ROOT / "csi-rx" / "main"
DUMP_SOURCE = Path(__file__).resolve().parent / "ari2_pack_dump.c"

PC_PARSER_ROOT = Path(
    "/home/kang/.workspace/csi-occupancy-breathing/external/csi-breathing-presence"
)
sys.path.insert(0, str(PC_PARSER_ROOT))

from csi_presence.ari2 import HEADER, MAGIC, parse_packet  # noqa: E402

PACKET_BYTES = 154
BINS = 57
K_OFFSET = 28
SUBCARRIERS = np.arange(-K_OFFSET, K_OFFSET + 1)  # k of bin 0..56

# Values written by ari2_pack_dump.c.
SYNTHETIC_REAL_OFFSET = 40
ROOM = 1
NODE = 1
CHANNEL = 36
BOOT_ID = 0xA1B2C3D4
FIRST_SEQ = 4_000_000_000
TIMESTAMP_US = 0xFFFFFFF0
RSSI = -61
AGC = 35
FFT = -3
UNIX_NS = 1_760_000_000_000_000_000


def build_and_run_dump() -> bytes:
    """Compile firmware packing code + dump program, return its stdout bytes."""
    compiler = shutil.which("cc") or shutil.which("gcc")
    assert compiler is not None, "no host C compiler"

    with tempfile.TemporaryDirectory() as work_dir:
        binary = Path(work_dir) / "ari2_pack_dump"
        command = [
            compiler,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{FIRMWARE_MAIN}",
            str(FIRMWARE_MAIN / "ari2_packet.c"),
            str(DUMP_SOURCE),
            "-o",
            str(binary),
        ]
        subprocess.run(command, check=True)

        result = subprocess.run([str(binary)], check=True, capture_output=True)

    return result.stdout


def expected_iq(first_word_invalid_bins: tuple[int, ...] = ()) -> np.ndarray:
    """(57, 2) int8 [imag, real] that the synthetic frame must map to."""
    iq = np.stack([SUBCARRIERS, SYNTHETIC_REAL_OFFSET + SUBCARRIERS], axis=1)

    iq[K_OFFSET] = 0  # DC bin is always zero-filled

    for bin_index in first_word_invalid_bins:
        iq[bin_index] = 0

    return iq.astype(np.int8)


def split_packets(stream: bytes) -> list[bytes]:
    assert len(stream) % PACKET_BYTES == 0, len(stream)

    packets = []

    for start in range(0, len(stream), PACKET_BYTES):
        packets.append(stream[start:start + PACKET_BYTES])

    return packets


def check_header(packet: bytes, seq: int) -> None:
    fields = HEADER.unpack_from(packet)
    (magic, room, node, channel, flags, boot, packet_seq, sample_seq, timestamp,
     unused_a, unused_b, rssi, agc, fft, tx_power, iq_bytes, bins) = fields

    assert magic == MAGIC
    assert (room, node, channel, flags) == (ROOM, NODE, CHANNEL, 0)
    assert boot == BOOT_ID
    assert packet_seq == seq
    assert sample_seq == seq
    assert timestamp == TIMESTAMP_US
    assert (unused_a, unused_b) == (0, 0)
    assert (rssi, agc, fft, tx_power) == (RSSI, AGC, FFT, 0)
    assert (iq_bytes, bins) == (2 * BINS, BINS)


def check_frame(packet: bytes, iq: np.ndarray) -> None:
    frame = parse_packet(packet, UNIX_NS, room=ROOM, bins=BINS)

    assert frame is not None, "PC parser rejected the packet"
    assert frame.room == ROOM
    assert frame.timestamp_us == TIMESTAMP_US
    assert (frame.agc, frame.fft, frame.tx_qdbm) == (AGC, FFT, 0)
    assert frame.iq.shape == (BINS, 2)
    np.testing.assert_array_equal(frame.iq, iq)


def test_packets_round_trip_through_pc_parser() -> None:
    packets = split_packets(build_and_run_dump())

    assert len(packets) == 3

    # 0: ascending source order, 1: DC-first source order. Both must give the
    # same ascending bins k = -28..28 with DC zeroed.
    check_header(packets[0], FIRST_SEQ)
    check_frame(packets[0], expected_iq())

    check_header(packets[1], FIRST_SEQ + 1)
    check_frame(packets[1], expected_iq())

    # 2: first_word_invalid -> source pairs 0, 1 (k = -28, -27) zeroed.
    check_header(packets[2], FIRST_SEQ + 2)
    check_frame(packets[2], expected_iq(first_word_invalid_bins=(0, 1)))


def test_pc_parser_drops_only_dc_bin() -> None:
    """The PC keeps bins that are non-zero in >99% of frames (to_arrays)."""
    from csi_presence.ari2 import to_arrays

    packets = split_packets(build_and_run_dump())
    frames = [parse_packet(packets[0], UNIX_NS + i, room=ROOM, bins=BINS) for i in range(10)]

    _csi, _t_us, coords, _key = to_arrays(frames)

    expected_coords = [b for b in range(BINS) if b != K_OFFSET]
    assert coords.tolist() == expected_coords


def test_wrong_bin_count_is_rejected_by_pc_parser() -> None:
    packets = split_packets(build_and_run_dump())

    assert parse_packet(packets[0], UNIX_NS, bins=117) is None


if __name__ == "__main__":
    test_packets_round_trip_through_pc_parser()
    test_pc_parser_drops_only_dc_bin()
    test_wrong_bin_count_is_rejected_by_pc_parser()
    print("ari2 host check: 3 tests passed")
