"""Send console commands to one caselink board. Usage: board_cmd.py COM5 "set link 0" apply"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import board
from cli.frames import FrameDecoder

port = sys.argv[1]
connection = board.open_port(port)
decoder = FrameDecoder()
try:
    for command in sys.argv[2:]:
        ok = board.send_command(connection, decoder, command)
        print(f"[{port}] {command!r} -> {'ok' if ok else 'FAILED'}")
finally:
    connection.close()
print("config now:", board.query_config(port))
