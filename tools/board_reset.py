"""Reboot one board through its USB serial reset line, the software form of unplugging it.

usage: board_reset.py PORT_OR_SERIAL

Holds RTS (EN low) for 0.2 s with DTR low, releases it, then prints how many bytes the board
sent in the next 2 s so a dead board and a live one look different. NVS settings are untouched,
and the breathing doll's port is refused by `breathing.ports`. Stop the recorder first: one
process owns a port. The recorder's stall watchdog runs the same pulse by itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breathing.ports import pulse_reset, resolve_port  # noqa: E402

port = resolve_port(sys.argv[1])
received = pulse_reset(port)
print(f"{port}: {received} bytes within 2 s after the reset pulse")
