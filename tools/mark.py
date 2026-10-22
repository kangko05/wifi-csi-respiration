"""Append a marker to a running recording. usage: mark.py <session_dir> <text...>"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breathing.markers import MARKERS_FILE, write_marker

session_dir = Path(sys.argv[1])
write_marker(session_dir, " ".join(sys.argv[2:]))
print("marked", session_dir / MARKERS_FILE)
