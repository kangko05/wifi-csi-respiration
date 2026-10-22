"""Record the ARI2 packets the main board sends over the main module's AP (UDP).

usage: record_udp.py NAME [--port 3334] [--bind 0.0.0.0] [--chunk-s 600] [--root recordings] [--session DIR]

The UDP counterpart of record_main.py: csi-main built with build_ap.sh sends each ARI2 packet
as one datagram, the same bytes its UART carries. Writes the same session folder
(chunk_<stamp>.ari.gz, session.txt, markers.txt), so the live judges and demo.py work unchanged.
There is no board to reset here: when nothing arrives for --stale-s seconds a marker is written
once, and another when packets come back.
"""

import argparse
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breathing.markers import write_marker
from cli.ari2 import ChunkWriter, parse_packet

STATUS_PERIOD_S = 60.0
FLUSH_PERIOD_S = 1.0
RECV_TIMEOUT_S = 0.5


def chunk_name(host_ns: int) -> str:
    return "chunk_" + datetime.fromtimestamp(host_ns / 1e9).strftime("%Y%m%d_%H%M%S")


def record(
    name: str,
    bind: str,
    port: int,
    chunk_s: float,
    stale_s: float,
    root: Path,
    session_dir: Path | None = None,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, port))
    sock.settimeout(RECV_TIMEOUT_S)

    if session_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = root / f"{stamp}_{name}"
        session_dir.mkdir(parents=True, exist_ok=False)

        with open(session_dir / "session.txt", "w", encoding="utf-8") as session:
            session.write(
                f"start\t{datetime.now().isoformat(timespec='milliseconds')}\n"
            )
            session.write(f"port\tudp:{bind}:{port}\n")
            session.write(f"name\t{name}\n")
            session.write("source\tcsi-main ari2 relay over AP\n")
    else:
        with open(session_dir / "session.txt", "a", encoding="utf-8") as session:
            session.write(
                f"resumed\t{datetime.now().isoformat(timespec='milliseconds')}\n"
            )

    writer = ChunkWriter(session_dir, chunk_s, chunk_name)
    write_marker(session_dir, f"recording started on udp {bind}:{port}")
    last_packet_s = time.monotonic()
    last_status_s = time.monotonic()
    last_flush_s = time.monotonic()
    packets_since_status = 0
    rejected = 0
    stale = False
    print(f"recording {session_dir}", flush=True)

    try:
        while True:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                data = None

            if data is not None:
                packet = parse_packet(data)

                if packet is None:
                    rejected += 1
                else:
                    writer.write(time.time_ns(), packet)
                    last_packet_s = time.monotonic()
                    packets_since_status += 1

                    if stale:
                        stale = False
                        write_marker(session_dir, "packets back")

            if not stale and time.monotonic() - last_packet_s > stale_s:
                stale = True
                write_marker(session_dir, f"no packets for {stale_s:.0f} s")
                print(f"no packets for {stale_s:.0f} s", flush=True)

            if time.monotonic() - last_flush_s >= FLUSH_PERIOD_S:
                writer.flush()
                last_flush_s = time.monotonic()

            if time.monotonic() - last_status_s >= STATUS_PERIOD_S:
                rate = packets_since_status / (time.monotonic() - last_status_s)
                print(
                    f"{datetime.now().strftime('%H:%M:%S')} {rate:.1f} pkt/s rejected={rejected}",
                    flush=True,
                )
                last_status_s = time.monotonic()
                packets_since_status = 0
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
        sock.close()
        write_marker(session_dir, "recorder stopped")


def _stop(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> None:
    # SIGTERM closes the gzip chunk the same way Ctrl+C does (a background job ignores SIGINT).
    signal.signal(signal.SIGTERM, _stop)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name")
    parser.add_argument("--port", type=int, default=3334)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--chunk-s", type=float, default=600.0)
    parser.add_argument("--stale-s", type=float, default=30.0)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "recordings",
    )
    parser.add_argument(
        "--session", type=Path, help="existing session folder to append to"
    )
    arguments = parser.parse_args()

    if (
        arguments.session is not None
        and not (arguments.session / "session.txt").exists()
    ):
        parser.error(f"{arguments.session} is not a session folder")

    record(
        arguments.name,
        arguments.bind,
        arguments.port,
        arguments.chunk_s,
        arguments.stale_s,
        arguments.root,
        arguments.session,
    )


if __name__ == "__main__":
    main()
