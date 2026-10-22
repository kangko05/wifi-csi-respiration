import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RECORDING = ROOT / "recordings" / "meeting_20261016_102602"
sys.path.insert(0, str(ROOT / "tools"))  # tests import the fake stream generator


def pytest_configure(config):
    config.addinivalue_line("markers", "replay: replays the 10-16 meeting recording; skipped when it is absent")


@pytest.fixture
def session_dir():
    """A fresh session folder. Plain tempfile rather than tmp_path: pytest's numbered temp
    root on this machine holds a dangling `pytest-current` link that fails its cleanup."""
    with tempfile.TemporaryDirectory() as path:
        yield Path(path)


@pytest.fixture(scope="session")
def recording() -> Path:
    if not (RECORDING / "main" / "frames.bin.gz").exists():
        pytest.skip(f"recording missing: {RECORDING}")

    return RECORDING
