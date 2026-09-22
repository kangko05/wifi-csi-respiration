"""Run the one-window respiration CLI from the repository root: `.venv/bin/python main.py --help`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from csi_respiration.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(prog="main.py"))
