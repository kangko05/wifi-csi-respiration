"""Read the main unit's UART (ARI2 relay mode) for a while and summarise packets per (room, node).

usage: ari2_watch.py PORT SECONDS [OUT.ari2.bin]
Header "<4sBBBBIIIIIIbBbbHH" = magic room node channel flags boot_id packet_seq sample_seq
timestamp_us unused unused rssi agc fft txpower iq_bytes bins (csi-rx/main/ari2_packet.h).
"""

import struct
import sys
import time
from collections import Counter, defaultdict

import serial

HEADER = struct.Struct("<4sBBBBIIIIIIbBbbHH")
PACKET_BYTES = 154
BAUD_RATE = 921600


def main(port, seconds, out_path=None):
    connection = serial.Serial()
    connection.port = port
    connection.baudrate = BAUD_RATE
    connection.timeout = 0.2
    connection.dtr = False
    connection.rts = False
    connection.open()

    buffer = bytearray()
    text = bytearray()
    packets = defaultdict(list)
    out = open(out_path, "wb") if out_path else None
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        buffer += connection.read(65536)

        while True:
            start = buffer.find(b"ARI2")
            if start < 0:
                text += buffer
                buffer.clear()
                break
            text += buffer[:start]
            del buffer[:start]
            if len(buffer) < PACKET_BYTES:
                break
            packet = bytes(buffer[:PACKET_BYTES])
            del buffer[:PACKET_BYTES]
            fields = HEADER.unpack_from(packet)
            if out:
                out.write(struct.pack("<QIH", time.time_ns(), 0, PACKET_BYTES) + packet)
            packets[(fields[1], fields[2])].append(fields)

    connection.close()
    if out:
        out.close()

    for line in text.decode("utf-8", "replace").splitlines():
        if "csi-main" in line or "ari2" in line:
            print("  text:", line.strip()[:120])

    if not packets:
        print(f"no ARI2 packets in {seconds} s")
        return

    for (room, node), rows in sorted(packets.items()):
        seqs = [row[6] for row in rows]
        gaps = sum(b - a - 1 for a, b in zip(seqs, seqs[1:]) if b > a + 1)
        gains = Counter((row[12], row[13]) for row in rows)
        rssi = sorted(row[11] for row in rows)
        print(f"room={room} node={node}: {len(rows)} pkts ({len(rows) / seconds:.1f}/s)  seq {seqs[0]}..{seqs[-1]} "
              f"gaps {gaps}  rssi median {rssi[len(rssi) // 2]}  gain pairs {len(gains)} top {gains.most_common(3)}  "
              f"boot_id {rows[0][5]}  bins {rows[0][16]}")


main(sys.argv[1], float(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
