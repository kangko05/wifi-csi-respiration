"""Find runs of bit-identical CSI in ARI2 recordings and describe each onset.

usage: freeze_scan.py SESSION_DIR [--min-run 50]
Run from the wifi-csi-respiration root (imports breathing.sources).
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")
from breathing.sources import read_ari2_recording


def clock(t):
    return dt.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def scan(session, node, min_run):
    records = list(read_ari2_recording(session, 1, node))
    if not records:
        return
    first, last = records[0].time, records[-1].time
    print(f"node {node}: {len(records)} packets {clock(first)}-{clock(last)} ({(last - first) / 60:.1f} min)")
    fresh_values = {}
    run_start = 0
    for i in range(1, len(records) + 1):
        if i < len(records) and np.array_equal(records[i].h, records[i - 1].h):
            continue
        length = i - run_start
        if length >= min_run:
            onset = records[run_start]
            before = records[max(0, run_start - 3):run_start]
            seen_at = fresh_values.get(onset.h.tobytes())
            print(f"  frozen {clock(onset.time)}-{clock(records[i - 1].time)} n={length} "
                  f"seq {onset.seq}->{records[i - 1].seq} rssi {sorted({r.rssi for r in records[run_start:i]})} "
                  f"gains {len({r.gain for r in records[run_start:i]})}")
            print("    before onset: " + ", ".join(f"{clock(r.time)} seq={r.seq} rssi={r.rssi} gain={r.gain}" for r in before))
            print(f"    onset value first seen as a fresh frame: {clock(seen_at) if seen_at else 'no'}")
            gap = [records[j].seq - records[j - 1].seq for j in range(run_start, min(run_start + 50, i))]
            print(f"    seq steps in first 50 frozen frames: {sorted(set(gap))}")
        fresh_values.setdefault(records[run_start].h.tobytes(), records[run_start].time)
        run_start = i


parser = argparse.ArgumentParser()
parser.add_argument("session", type=Path)
parser.add_argument("--min-run", type=int, default=50)
args = parser.parse_args()
for node in (1, 2):
    scan(args.session, node, args.min_run)
