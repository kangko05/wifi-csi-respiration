"""Packets per (room, node) in the last N seconds of a recording that is still being written.

usage: stream_check.py <session_dir> [seconds]
Reads the newest chunk; a chunk cut off mid-record (power loss) is read up to the cut.
"""

import gzip
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli.ari2 import RECORD, parse_packet

session_dir = Path(sys.argv[1])
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
chunks = sorted(session_dir.glob("chunk_*.ari.gz"))

if not chunks:
    sys.exit(f"no chunks in {session_dir}")

newest = chunks[-1]
cutoff_ns = time.time_ns() - int(seconds * 1e9)
counts = defaultdict(int)
seqs = defaultdict(list)
rssi = defaultdict(list)
last_ns = 0

with gzip.open(newest, "rb") as file:
    try:
        while True:
            header = file.read(RECORD.size)

            if len(header) < RECORD.size:
                break

            host_ns, _, length = RECORD.unpack(header)
            packet = parse_packet(file.read(length))

            if packet is None:
                break

            last_ns = host_ns

            if host_ns < cutoff_ns:
                continue

            counts[packet.stream] += 1
            seqs[packet.stream].append(packet.packet_seq)
            rssi[packet.stream].append(packet.rssi)
    except EOFError:
        pass

age_s = (time.time_ns() - last_ns) / 1e9 if last_ns else float("nan")
print(f"{newest.name}: last packet {age_s:.0f} s ago (flushed every 60 s); window {seconds:.0f} s")

for stream in sorted(counts):
    sequence = seqs[stream]
    gaps = sum(b - a - 1 for a, b in zip(sequence, sequence[1:]) if b > a + 1)
    median_rssi = sorted(rssi[stream])[len(rssi[stream]) // 2]
    print(f"  {stream}: {counts[stream]} pkts ({counts[stream] / seconds:.1f}/s) gaps {gaps} rssi {median_rssi}")

if not counts:
    print("  no packets inside the window")
