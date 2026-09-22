"""`python -m csi_respiration` (with `src` on PYTHONPATH); see `csi_respiration.cli`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(prog="python -m csi_respiration"))
