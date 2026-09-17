"""Serial helpers: enumeration without opening, and a quiet RX port open.

The open path builds `serial.Serial(port=None)` first and clears DTR/RTS before
assigning the port, so the collector itself does not assert the reset/boot lines
and never writes to the device. This is a best effort only: the OS driver may
still toggle the control lines when the handle is opened, so a board reset at
open time cannot be ruled out.
"""

from __future__ import annotations

from typing import Any, Callable

PYSERIAL_HINT = (
    "pyserial is required. Install it into the project venv:\n"
    "  python -m venv .venv\n"
    "  .venv/Scripts/python.exe -m pip install --no-cache-dir pyserial==3.5\n"
    "and run the collector with .venv/Scripts/python.exe"
)


class MissingDependency(RuntimeError):
    pass


def _import_serial():
    try:
        import serial  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError as exc:  # pragma: no cover - exercised via injection in tests
        raise MissingDependency(PYSERIAL_HINT) from exc
    return serial


def list_ports(comports: Callable[..., list] | None = None) -> list[dict]:
    """Enumerate serial ports. Enumeration does not open any port."""
    if comports is None:
        try:
            from serial.tools import list_ports as tools  # noqa: PLC0415
        except ImportError as exc:
            raise MissingDependency(PYSERIAL_HINT) from exc
        comports = tools.comports
    return [
        {
            "device": getattr(p, "device", ""),
            "description": getattr(p, "description", ""),
            "hwid": getattr(p, "hwid", ""),
        }
        for p in comports()
    ]


def open_serial(
    port: str,
    baud: int,
    *,
    timeout: float,
    serial_factory: Callable[..., Any] | None = None,
) -> Any:
    """Open one explicit port. No role guessing, no writes, no buffer clearing.

    `reset_input_buffer()` is deliberately not called, so the application never
    discards buffered bytes itself: everything `read()` returns is recorded,
    including whatever was already queued when the port was opened. This is not
    a promise that bytes queued before/at open survive — the OS driver and the
    open call itself may buffer or drop data before the first read.
    """
    factory = serial_factory
    if factory is None:
        factory = _import_serial().Serial
    handle = factory(port=None, baudrate=baud, timeout=timeout)
    handle.dtr = False
    handle.rts = False
    handle.port = port
    handle.open()
    return handle


def describe_settings(handle: Any) -> dict:
    """Host-side view of the port settings actually in effect."""
    return {
        "port": getattr(handle, "port", None),
        "baudrate": getattr(handle, "baudrate", None),
        "bytesize": getattr(handle, "bytesize", None),
        "parity": getattr(handle, "parity", None),
        "stopbits": getattr(handle, "stopbits", None),
        "timeout": getattr(handle, "timeout", None),
        "dtr_requested": False,
        "rts_requested": False,
        "input_buffer_cleared": False,
        "note": (
            "The OS driver may still toggle DTR/RTS on open; a board reset at open time is not ruled out. "
            "No application-side input flush is performed, but bytes sent before the first read may still "
            "be dropped by driver/open buffering."
        ),
    }
