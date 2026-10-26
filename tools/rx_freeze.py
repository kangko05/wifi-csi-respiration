"""Find bit-identical CSI runs in a caselink RX recording (frames.bin.gz) and show the
radio header around each onset.

usage: rx_freeze.py SESSION_DIR [--min-run 50] [--summary]

CSI frame payload = csi_record_header_t (firmware/caselink/main/radio.h) + CSI bytes
+ float compensate_gain. Header times are the board's rx_ctrl timestamp in microseconds.
"""

import argparse
import struct
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

from cli.frames import FRAME_TYPE_CSI, FRAME_TYPE_STATUS
from cli.recording import read_frames

HEADER = struct.Struct("<IIHbbBbBBBBBBH")
FIELDS = ("rx_count", "timestamp_us", "rx_seq", "rssi", "noise_floor", "agc", "fft",
          "channel", "second", "bb_format", "rate", "flags", "boot_id", "csi_length")


def clock(host_ns: int) -> str:
    return datetime.fromtimestamp(host_ns / 1e9).strftime("%H:%M:%S.%f")[:-3]


def load(session: Path):
    records = []
    statuses = []

    for host_ns, frame in read_frames(session / "frames.bin.gz"):
        if frame.frame_type == FRAME_TYPE_STATUS:
            statuses.append((host_ns, frame.payload.decode("utf-8", errors="replace")))
            continue

        if frame.frame_type != FRAME_TYPE_CSI or len(frame.payload) < HEADER.size:
            continue

        header = dict(zip(FIELDS, HEADER.unpack_from(frame.payload)))
        csi = bytes(frame.payload[HEADER.size:HEADER.size + header["csi_length"]])
        header["host_ns"] = host_ns
        header["csi"] = csi
        records.append(header)

    return records, statuses


def describe(record) -> str:
    return (f"{clock(record['host_ns'])} n={record['rx_count']} sq={record['rx_seq']} "
            f"rssi={record['rssi']} nf={record['noise_floor']} agc={record['agc']} fft={record['fft']} "
            f"bb={record['bb_format']} rate={record['rate']} fl={record['flags']:#x} len={record['csi_length']}")


def runs(records, min_run):
    start = 0

    for index in range(1, len(records) + 1):
        if index < len(records) and records[index]["csi"] == records[index - 1]["csi"]:
            continue

        if index - start >= min_run:
            yield start, index

        start = index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path)
    parser.add_argument("--min-run", type=int, default=50)
    parser.add_argument("--summary", action="store_true")
    arguments = parser.parse_args()

    records, statuses = load(arguments.session)

    if not records:
        print("no CSI records")
        return 1

    span_min = (records[-1]["host_ns"] - records[0]["host_ns"]) / 60e9
    print(f"{len(records)} CSI records {clock(records[0]['host_ns'])}-{clock(records[-1]['host_ns'])} ({span_min:.1f} min)")
    print("overall bb_format", dict(Counter(r["bb_format"] for r in records)),
          "rate", dict(Counter(r["rate"] for r in records)),
          "flags", dict(Counter(r["flags"] for r in records)),
          "len", dict(Counter(r["csi_length"] for r in records)))

    frozen_total = 0

    for start, end in runs(records, arguments.min_run):
        inside = records[start:end]
        frozen_total += len(inside)
        print(f"\nFROZEN {clock(inside[0]['host_ns'])}-{clock(inside[-1]['host_ns'])} frames={len(inside)}")
        print("  rssi", sorted({r["rssi"] for r in inside}),
              "nf", sorted({r["noise_floor"] for r in inside}),
              "bb", dict(Counter(r["bb_format"] for r in inside)),
              "rate", dict(Counter(r["rate"] for r in inside)),
              "flags", dict(Counter(r["flags"] for r in inside)),
              "rx_count gaps", sorted(Counter(b["rx_count"] - a["rx_count"] for a, b in zip(inside, inside[1:])).items())[:5])

        for record in records[max(0, start - 3):start]:
            print("  before ", describe(record))

        for record in inside[:3]:
            print("  frozen ", describe(record))

        if end < len(records):
            print("  release", describe(records[end]))

        onset_ns = inside[0]["host_ns"]
        nearby = [text for host_ns, text in statuses if abs(host_ns - onset_ns) < 2.5e9]

        for text in nearby:
            print("  status ", text)

    print(f"\nfrozen frames {frozen_total} / {len(records)}")

    if arguments.summary:
        bucket = {}

        for record in records:
            key = record["host_ns"] // 10_000_000_000
            count, distinct = bucket.get(key, (0, set()))
            distinct.add(record["csi"])
            bucket[key] = (count + 1, distinct)

        for key, (count, distinct) in sorted(bucket.items()):
            print(f"{clock(key * 10_000_000_000)} pkts {count} distinct {len(distinct)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
