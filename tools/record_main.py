"""Record the main board's ARI2 USB stream overnight.

usage: record_main.py PORT NAME [--chunk-s 600] [--stale-s 30] [--root recordings] [--session DIR]

PORT is a COM name or the board's USB serial number (recommended: the COM number changes
with the USB socket, the serial does not). The main board is A2DF0F2C38BDF011B7C5137148E9DE0F.

Writes recordings/<YYYYMMDD_HHMMSS>_<NAME>/chunk_<stamp>.ari.gz (records "<QIH" + packet,
the same layout as the 10-13 and 10-18 night recordings), session.txt, markers.txt.
When no packet arrives for --stale-s seconds the port is closed, the board is rebooted through
its reset line (reopening alone never brought a stalled board back: 4 stalls on 10-19 night),
the port is reopened and a "watchdog restart" marker is written. Add markers with tools/mark.py.
`--session DIR` appends to an existing session (after the recorder was stopped to reset the
board by hand) instead of creating a new folder; the live judges keep tailing the same folder.
The gzip chunk is flushed every FLUSH_PERIOD_S so `python -m breathing live` can tail it
about a second behind the board.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breathing.markers import write_marker
from breathing.ports import ProtectedPortError, open_port, pulse_reset
from cli.ari2 import Ari2Decoder, ChunkWriter

STATUS_PERIOD_S = 60.0
FLUSH_PERIOD_S = 1.0
REOPEN_RETRY_S = 5.0


def reopen_until_present(port_or_serial: str) -> serial.Serial:
    """Keeps trying while the board is unplugged (e.g. being moved to another room)."""
    while True:
        try:
            return open_port(port_or_serial)
        except ProtectedPortError:
            raise
        except (serial.SerialException, RuntimeError, OSError) as error:
            print(f"port not ready ({error}); retrying in {REOPEN_RETRY_S:.0f} s", flush=True)
            time.sleep(REOPEN_RETRY_S)


def chunk_name(host_ns: int) -> str:
    return "chunk_" + datetime.fromtimestamp(host_ns / 1e9).strftime("%Y%m%d_%H%M%S")


def record(port: str, name: str, chunk_s: float, stale_s: float, root: Path, session_dir: Path | None = None) -> None:
    if session_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = root / f"{stamp}_{name}"
        session_dir.mkdir(parents=True, exist_ok=False)

        with open(session_dir / "session.txt", "w", encoding="utf-8") as session:
            session.write(f"start\t{datetime.now().isoformat(timespec='milliseconds')}\n")
            session.write(f"port\t{port}\n")
            session.write(f"name\t{name}\n")
            session.write("source\tcsi-main ari2 relay\n")
    else:
        with open(session_dir / "session.txt", "a", encoding="utf-8") as session:
            session.write(f"resumed\t{datetime.now().isoformat(timespec='milliseconds')}\n")

    writer = ChunkWriter(session_dir, chunk_s, chunk_name)
    decoder = Ari2Decoder()
    connection = reopen_until_present(port)
    write_marker(session_dir, f"recording started on {connection.port}")
    last_packet_s = time.monotonic()
    last_status_s = time.monotonic()
    last_flush_s = time.monotonic()
    packets_since_status = 0
    restarts = 0
    print(f"recording {session_dir}", flush=True)

    try:
        while True:
            try:
                data = connection.read(65536)
            except (serial.SerialException, OSError) as error:
                restarts += 1
                write_marker(session_dir, f"watchdog restart #{restarts}: read failed ({error}); waiting for the port")
                print(f"read failed: {error}", flush=True)
                connection.close()
                connection = reopen_until_present(port)
                decoder = Ari2Decoder()
                last_packet_s = time.monotonic()
                continue

            host_ns = time.time_ns()
            packets = decoder.feed(data) if data else []

            for packet in packets:
                writer.write(host_ns, packet)

            if packets:
                last_packet_s = time.monotonic()
                packets_since_status += len(packets)

            if time.monotonic() - last_packet_s > stale_s:
                restarts += 1
                connection.close()
                time.sleep(1.0)
                try:
                    received = pulse_reset(port)
                    pulse = f"reset pulse: {received} bytes within 2 s"
                except (serial.SerialException, RuntimeError, OSError) as error:
                    pulse = f"reset pulse failed ({error})"
                write_marker(session_dir, f"watchdog restart #{restarts}: no packets for {stale_s:.0f} s; {pulse}; port reopened")
                print(f"watchdog restart #{restarts}: {pulse}", flush=True)
                connection = reopen_until_present(port)
                decoder = Ari2Decoder()
                last_packet_s = time.monotonic()

            if time.monotonic() - last_flush_s >= FLUSH_PERIOD_S:
                writer.flush()
                last_flush_s = time.monotonic()

            if time.monotonic() - last_status_s >= STATUS_PERIOD_S:
                rate = packets_since_status / (time.monotonic() - last_status_s)
                print(f"{datetime.now().strftime('%H:%M:%S')} {rate:.1f} pkt/s restarts={restarts}", flush=True)
                last_status_s = time.monotonic()
                packets_since_status = 0
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
        connection.close()
        write_marker(session_dir, "recorder stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port")
    parser.add_argument("name")
    parser.add_argument("--chunk-s", type=float, default=600.0)
    parser.add_argument("--stale-s", type=float, default=30.0)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent / "recordings")
    parser.add_argument("--session", type=Path, help="existing session folder to append to")
    arguments = parser.parse_args()

    if arguments.session is not None and not (arguments.session / "session.txt").exists():
        parser.error(f"{arguments.session} is not a session folder")

    record(arguments.port, arguments.name, arguments.chunk_s, arguments.stale_s, arguments.root, arguments.session)


if __name__ == "__main__":
    main()
