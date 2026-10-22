"""Print each caselink board's config via the frames console. Usage: board_query.py COM3 COM5 ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import board

for port in sys.argv[1:]:
    print(f"=== {port} ===")
    try:
        text = board.query_config(port)
    except Exception as error:
        print("  error:", error)
        continue
    print("  config:", text)
