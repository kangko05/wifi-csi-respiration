"""Serial ports of the boards. Opening is by COM name or by the board's USB serial number
(the COM number changes with the USB socket, the serial does not). The breathing doll's
port is never opened: it would reset the doll.
"""

import os
import time

import serial
from serial.tools import list_ports

BAUD_RATE = 921600
READ_TIMEOUT_S = 0.2
DOLL_SERIAL = "76e1e2612cbdf011bcbe107148e9de0f"


class ProtectedPortError(RuntimeError):
    """The breathing doll's port. Never retried, never opened."""


def resolve_port(port_or_serial: str) -> str:
    """COM name as given, or the COM name of the board with this USB serial (raises if absent)."""
    if port_or_serial.upper().startswith("COM"):
        for info in list_ports.comports():
            if info.device.upper() == port_or_serial.upper() and (info.serial_number or "").lower() == DOLL_SERIAL:
                raise ProtectedPortError("refusing to open the breathing doll's port")

        return port_or_serial

    wanted = port_or_serial.lower()

    if wanted == DOLL_SERIAL:
        raise ProtectedPortError("refusing to open the breathing doll's port")

    for info in list_ports.comports():
        if (info.serial_number or "").lower() == wanted:
            return info.device

    raise RuntimeError(f"no board with USB serial {port_or_serial}")


def open_port(port_or_serial: str) -> serial.Serial:
    connection = serial.Serial()
    connection.port = resolve_port(port_or_serial)
    connection.baudrate = BAUD_RATE
    connection.timeout = READ_TIMEOUT_S

    if os.name == "posix":
        # Linux raises DTR and RTS together on open, which leaves the board running. Lowering
        # them afterwards reset the board on every open (10-23), and so did the hangup on close.
        clear_hangup_on_close(connection.port)
    else:
        # DTR/RTS low so opening the port does not hold the board in reset.
        connection.dtr = False
        connection.rts = False

    connection.open()
    return connection


def clear_hangup_on_close(device: str) -> None:
    """Turn off HUPCL so closing the port leaves DTR/RTS up (stty -hupcl). Lasts until replug."""
    import termios

    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    try:
        attributes = termios.tcgetattr(fd)
        attributes[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    finally:
        os.close(fd)


def pulse_reset(port_or_serial: str, settle_s: float = 1.5, listen_s: float = 2.0) -> int:
    """Reboot the board through the USB serial reset line (RTS low pulls EN low for 0.2 s with
    DTR low), the software form of unplugging it. NVS settings are untouched. Returns how many
    bytes the board sent in the next `listen_s` so a dead board and a live one look different.
    The port must be closed by everyone else: one process owns a port."""
    connection = serial.Serial()
    connection.port = resolve_port(port_or_serial)
    connection.baudrate = BAUD_RATE
    connection.dtr = False
    connection.rts = False
    connection.open()
    try:
        connection.rts = True
        time.sleep(0.2)
        connection.rts = False
        time.sleep(settle_s)
        connection.timeout = listen_s
        return len(connection.read(4096))
    finally:
        connection.close()
