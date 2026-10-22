import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial
from serial.tools import list_ports

from cli.ari2 import Ari2Decoder, ChunkWriter
from cli.frames import FRAME_TYPE_CSI, FRAME_TYPE_STATUS, FRAME_TYPE_TEXT, FrameDecoder
from cli.recording import FrameWriter

BAUD_RATE = 921600
REPLY_TIMEOUT_S = 3.0
RESTART_TIMEOUT_S = 8.0
BOOT_SETTLE_S = 2.0
SUMMARY_PERIOD_S = 10.0
MAIN_CHUNK_S = 600.0    # same 10-minute chunks as the 10-13 night on ksrv
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"

# 호흡 인형 ESP: 절대 열지 않음
PROTECTED_SERIAL_NUMBERS = {"76e1e2612cbdf011bcbe107148e9de0f"}


def refuse_protected_port(port: str) -> None:
    real_path = str(Path(port).resolve())
    for info in list_ports.comports():
        if info.serial_number in PROTECTED_SERIAL_NUMBERS and str(Path(info.device).resolve()) == real_path:
            raise SystemExit(f"refusing to open {port}: protected device ({info.serial_number})")


def open_port(port: str) -> serial.Serial:
    refuse_protected_port(port)
    # DTR/RTS low: on the usual devkit auto-reset circuit, toggling them resets the chip.
    connection = serial.Serial()
    connection.port = port
    connection.baudrate = BAUD_RATE
    connection.timeout = 0.1
    connection.dtr = False
    connection.rts = False
    connection.open()
    return connection


def wait_for_boot(connection, decoder, echo=True) -> None:
    # 포트를 열 때 CP2102N 핸드셰이크 선 때문에 리셋될 수 있어서 "config " 배너를 기다림
    read_text_until(connection, decoder, lambda text: text.startswith("config "), BOOT_SETTLE_S, echo)


def read_text_until(connection, decoder, done, timeout_s, echo=True) -> list[str]:
    deadline = time.monotonic() + timeout_s
    lines = []
    while time.monotonic() < deadline:
        data = connection.read(4096)
        for frame in decoder.feed(data):
            if frame.frame_type != FRAME_TYPE_TEXT:
                continue
            text = frame.payload.decode("utf-8", errors="replace")
            lines.append(text)
            if echo:
                print(text)
            if done(text):
                return lines
    return lines


def send_command(connection, decoder, command: str) -> bool:
    connection.write((command + "\n").encode())

    is_restart = command.split()[0] in ("apply", "restart")

    if is_restart:
        # 재시작 계열은 "config " 배너가 다시 나오면 성공
        lines = read_text_until(connection, decoder, lambda t: t.startswith("config "), RESTART_TIMEOUT_S)
        return any(line.startswith("config ") for line in lines)

    # 일반 명령은 "ok" / "err" 응답
    lines = read_text_until(connection, decoder, lambda t: t.startswith(("ok", "err")), REPLY_TIMEOUT_S)

    return any(line.startswith("ok") for line in lines)


def query_config(port: str) -> str | None:
    """Opens the port, sends `get` and returns the config text after "ok "
    (e.g. "role=tx link=1 ch=36 ..."), or None when the board did not answer."""
    connection = open_port(port)
    decoder = FrameDecoder()

    try:
        wait_for_boot(connection, decoder, echo=False)
        connection.write(b"get\n")
        lines = read_text_until(connection, decoder, lambda t: t.startswith(("ok", "err")), REPLY_TIMEOUT_S, echo=False)
    finally:
        connection.close()

    for line in lines:
        if line.startswith("ok "):
            return line[len("ok "):]

    return None


def parse_role(config_text: str) -> str:
    for field in config_text.split():
        if field.startswith("role="):
            return field[len("role="):]

    return "?"


def command_ports(arguments) -> int:
    for port in list_ports.comports():
        # Skip legacy /dev/ttyS* entries with no hardware behind them.
        if port.description == "n/a":
            continue

        # Never open the breathing doll's port, not even to read its role.
        if port.serial_number in PROTECTED_SERIAL_NUMBERS:
            role = "protected"
        else:
            config_text = query_config(port.device)
            role = parse_role(config_text) if config_text is not None else "no-reply"

        print(f"{port.device}\t{role}\t{port.description}\t{port.serial_number}")

    return 0


def command_cmd(arguments) -> int:
    connection = open_port(arguments.port)
    decoder = FrameDecoder()
    wait_for_boot(connection, decoder)

    for command in arguments.commands:
        print(f"> {command}")
        if not send_command(connection, decoder, command):
            print(f"no ok reply for: {command}", file=sys.stderr)
            return 1
    return 0


def read_markers(markers_path: Path, stop: threading.Event) -> None:
    """Each line typed on stdin becomes a timestamped marker; an empty line is ignored."""
    with markers_path.open("a") as markers_file:
        while not stop.is_set():
            line = sys.stdin.readline()

            if line == "":
                return

            label = line.strip()

            if not label:
                continue

            now_ns = time.time_ns()
            iso_time = datetime.now().isoformat(timespec="milliseconds")
            markers_file.write(f"{now_ns}\t{iso_time}\t{label}\n")
            markers_file.flush()
            print(f"marker {iso_time} {label}")


def record_loop(connection, decoder, writer, stop: threading.Event) -> None:
    csi_count = 0
    last_summary = time.monotonic()
    last_status = ""

    while not stop.is_set():
        data = connection.read(8192)
        host_ns = time.time_ns()

        for frame in decoder.feed(data):
            writer.write(host_ns, frame)

            if frame.frame_type == FRAME_TYPE_CSI:
                csi_count += 1
            elif frame.frame_type == FRAME_TYPE_STATUS:
                last_status = frame.payload.decode("utf-8", errors="replace")
            elif frame.frame_type == FRAME_TYPE_TEXT:
                print(frame.payload.decode("utf-8", errors="replace"))

        free_text = decoder.take_free_text().strip()

        if free_text:
            print(f"[raw] {free_text}")

        now = time.monotonic()

        if now - last_summary >= SUMMARY_PERIOD_S:
            rate = csi_count / (now - last_summary)
            print(f"csi {rate:6.1f}/s  crc_err {decoder.crc_errors}  {last_status}")
            writer.flush()
            csi_count = 0
            last_summary = now


def command_record(arguments) -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(arguments.dir) / f"{stamp}_{arguments.name}"
    session_dir.mkdir(parents=True)

    connection = open_port(arguments.port)
    decoder = FrameDecoder()
    wait_for_boot(connection, decoder)

    print("> get")
    connection.write(b"get\n")
    config_lines = read_text_until(connection, decoder, lambda text: text.startswith("ok"), REPLY_TIMEOUT_S)

    with (session_dir / "session.txt").open("w") as session_file:
        session_file.write(f"start\t{datetime.now().isoformat(timespec='milliseconds')}\n")
        session_file.write(f"port\t{arguments.port}\n")
        session_file.write(f"name\t{arguments.name}\n")

        for line in config_lines:
            session_file.write(f"board\t{line}\n")

    writer = FrameWriter(session_dir / "frames.bin.gz")
    stop = threading.Event()

    recorder = threading.Thread(target=record_loop, args=(connection, decoder, writer, stop), daemon=True)
    recorder.start()

    marker_thread = threading.Thread(target=read_markers, args=(session_dir / "markers.txt", stop), daemon=True)
    marker_thread.start()

    print(f"recording to {session_dir}  (type a label + Enter for a marker, Ctrl+C to stop)")

    try:
        while recorder.is_alive():
            recorder.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        recorder.join(timeout=2.0)
        writer.close()
        connection.close()

    print(f"saved {session_dir}")
    return 0


def record_main_loop(connection, decoder, writer, stop: threading.Event) -> None:
    counts = {}
    last_summary = time.monotonic()

    while not stop.is_set():
        data = connection.read(65536)
        host_ns = time.time_ns()

        for packet in decoder.feed(data):
            writer.write(host_ns, packet)
            counts[packet.stream] = counts.get(packet.stream, 0) + 1

        free_text = decoder.take_free_text().strip()

        if free_text:
            print(f"[main] {free_text}")

        now = time.monotonic()

        if now - last_summary >= SUMMARY_PERIOD_S:
            elapsed = now - last_summary
            rates = "  ".join(f"{stream} {count / elapsed:5.1f}/s" for stream, count in sorted(counts.items()))
            print(f"{datetime.now():%H:%M:%S}  {rates or 'no packets'}  bad {decoder.bad_packets}")
            writer.flush()
            counts = {}
            last_summary = now


def command_record_main(arguments) -> int:
    """Record the main unit's ARI2 relay stream (all rooms, both nodes) as .ari.gz chunks."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(arguments.dir) / f"{stamp}_{arguments.name}"
    session_dir.mkdir(parents=True)

    with (session_dir / "session.txt").open("w") as session_file:
        session_file.write(f"start\t{datetime.now().isoformat(timespec='milliseconds')}\n")
        session_file.write(f"port\t{arguments.port}\n")
        session_file.write(f"name\t{arguments.name}\n")
        session_file.write("source\tcsi-main ari2 relay\n")

    # The main unit prints its boot log as text and then switches to raw packets; opening
    # the port resets it, so the first seconds are that log.
    connection = open_port(arguments.port)
    decoder = Ari2Decoder()
    writer = ChunkWriter(session_dir, arguments.chunk_s,
                         lambda host_ns: datetime.fromtimestamp(host_ns / 1e9).strftime("chunk_%Y%m%d_%H%M%S"))
    stop = threading.Event()

    recorder = threading.Thread(target=record_main_loop, args=(connection, decoder, writer, stop), daemon=True)
    recorder.start()

    marker_thread = threading.Thread(target=read_markers, args=(session_dir / "markers.txt", stop), daemon=True)
    marker_thread.start()

    print(f"recording main unit to {session_dir}  (type a label + Enter for a marker, Ctrl+C to stop)")

    try:
        while recorder.is_alive():
            recorder.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        recorder.join(timeout=2.0)
        writer.close()
        connection.close()

    print(f"saved {session_dir}")
    return 0

