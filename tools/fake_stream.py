"""Write a synthetic csi-main session (chunks + markers) for rehearsing the live tools.

usage: fake_stream.py SESSION_DIR [--realtime] [--baseline-s 120] [--breathe-s 330]
                      [--pauses 120,240] [--pause-s 45] [--chunk-s 120] [--room 1] [--node 2]

The scenario is the demo script: noise only for the Quiet baseline, then a 15/min doll with
breath holds of --pause-s at the --pauses offsets (seconds after the doll went on). Markers
`기준선 시작`, `기준선 끝`, `인형 켬`, `멈춤`, `재개` are written at those moments. With
--realtime the records are written at wall-clock pace and flushed every second, as the real
recorder does, so `python -m breathing live SESSION_DIR` can tail it.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from breathing.markers import BASELINE_END, BASELINE_START, DOLL_ON, PAUSE, RESUME, write_marker
from breathing.sources import ARI2_HEADER, CsiRecord, synthetic_records
from cli.ari2 import ChunkWriter, parse_packet

BREATHS_PER_MIN = 15.0
HT20_BINS = 57
DC_BIN = HT20_BINS // 2
FLUSH_PERIOD_S = 1.0


def ari2_packet(record: CsiRecord, room: int, node: int, timestamp_us: int) -> bytes:
    """One ARI2 packet whose IQ decodes back to `record.h` (DC bin re-inserted as zero)."""
    pairs = np.zeros((HT20_BINS, 2), dtype=np.int8)
    h = np.insert(record.h, DC_BIN, 0.0)
    pairs[:, 0] = np.clip(np.round(h.imag), -128, 127)
    pairs[:, 1] = np.clip(np.round(h.real), -128, 127)
    header = ARI2_HEADER.pack(b"ARI2", room, node, 36, 0, 1, record.seq, record.seq, timestamp_us & 0xFFFFFFFF,
                              0, 0, record.rssi, record.gain[0], record.gain[1], 0, 2 * HT20_BINS, HT20_BINS)
    return header + pairs.tobytes()


def chunk_name(host_ns: int) -> str:
    return "chunk_" + datetime.fromtimestamp(host_ns / 1e9).strftime("%Y%m%d_%H%M%S")


def write_fake_session(session_dir: Path, *, baseline_s: float = 120.0, breathe_s: float = 330.0,
                       pauses: tuple[tuple[float, float], ...] = ((120.0, 165.0), (240.0, 285.0)),
                       chunk_s: float = 120.0, realtime: bool = False, room: int = 1, node: int = 2,
                       seed: int = 0, start: float | None = None) -> float:
    """Returns the session's start time (unix seconds). `pauses` are offsets from the doll
    going on, which happens `baseline_s` after the start."""
    session_dir.mkdir(parents=True, exist_ok=True)
    start = time.time() if start is None else start
    doll_on = start + baseline_s
    events = [(start, BASELINE_START), (doll_on, BASELINE_END), (doll_on, DOLL_ON)]
    events += [(doll_on + a, PAUSE) for a, _ in pauses] + [(doll_on + b, RESUME) for _, b in pauses]
    events.sort()

    quiet = synthetic_records(baseline_s, None, start=start, seed=seed)
    doll = synthetic_records(breathe_s, BREATHS_PER_MIN, pauses=pauses, start=doll_on, seed=seed)
    writer = ChunkWriter(session_dir, chunk_s, chunk_name)
    last_flush = time.monotonic()

    for source in (quiet, doll):
        for record in source:
            if realtime:
                time.sleep(max(0.0, record.time - time.time()))

            while events and events[0][0] <= record.time:
                at, label = events.pop(0)
                write_marker(session_dir, label, now_ns=int(at * 1e9))

            packet = parse_packet(ari2_packet(record, room, node, int((record.time - start) * 1e6)))
            writer.write(int(record.time * 1e9), packet)

            if realtime and time.monotonic() - last_flush >= FLUSH_PERIOD_S:
                writer.flush()
                last_flush = time.monotonic()

    writer.close()
    return start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--baseline-s", type=float, default=120.0)
    parser.add_argument("--breathe-s", type=float, default=330.0)
    parser.add_argument("--pauses", default="120,240", help="pause starts, seconds after the doll goes on")
    parser.add_argument("--pause-s", type=float, default=45.0)
    parser.add_argument("--chunk-s", type=float, default=120.0)
    parser.add_argument("--room", type=int, default=1)
    parser.add_argument("--node", type=int, default=2)
    args = parser.parse_args()
    pauses = tuple((float(a), float(a) + args.pause_s) for a in args.pauses.split(",") if a)

    print(f"writing {args.session_dir} ({'real time' if args.realtime else 'instant'})", flush=True)
    start = write_fake_session(args.session_dir, baseline_s=args.baseline_s, breathe_s=args.breathe_s, pauses=pauses,
                               chunk_s=args.chunk_s, realtime=args.realtime, room=args.room, node=args.node)
    print(f"done, started {datetime.fromtimestamp(start).isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
