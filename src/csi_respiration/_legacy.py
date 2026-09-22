"""Import the byte-identical vendored legacy package without installing it.

`vendor/wifi-csi-proto/src` is appended to `sys.path` only if `csi_pipeline`
is not importable yet. A `csi_pipeline` from anywhere else is refused rather
than silently mixed with the vendored snapshot.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_ROOT = REPO_ROOT / "vendor" / "wifi-csi-proto"
VENDOR_SRC = VENDOR_ROOT / "src"


def _load():
    loaded = sys.modules.get("csi_pipeline")
    if loaded is None:
        if not (VENDOR_SRC / "csi_pipeline" / "__init__.py").is_file():
            raise ImportError(f"vendored legacy package not found under {VENDOR_SRC}")

        if str(VENDOR_SRC) not in sys.path:
            sys.path.append(str(VENDOR_SRC))

        loaded = importlib.import_module("csi_pipeline")

    origin = Path(loaded.__file__).resolve()
    if not origin.is_relative_to(VENDOR_SRC.resolve()):
        raise ImportError(
            f"csi_pipeline resolved to {origin}, not the vendored snapshot {VENDOR_SRC}"
        )

    return loaded


_load()

from csi_pipeline.gain import GainConfig, correct_gain  # noqa: E402
from csi_pipeline.input import (  # noqa: E402
    CAPTURE_BYTES,
    CAPTURE_RX_FORMAT,
    SessionData,
)
from csi_pipeline.periodicity import (  # noqa: E402
    PeriodicityConfig,
    PeriodicityWindow,
    estimate_periodicity,
    summarize_periodicity,
)
from csi_pipeline.phase_cir import (  # noqa: E402
    PhaseCirConfig,
    PhaseCirDecision,
    PhaseCirUnavailable,
    run_phase_cir,
)
from csi_pipeline.quality import QualityConfig, assess_quality  # noqa: E402

__all__ = [
    "CAPTURE_BYTES", "CAPTURE_RX_FORMAT", "GainConfig", "PeriodicityConfig",
    "PeriodicityWindow", "PhaseCirConfig", "PhaseCirDecision", "PhaseCirUnavailable",
    "QualityConfig", "SessionData", "VENDOR_ROOT", "assess_quality", "correct_gain",
    "estimate_periodicity", "run_phase_cir", "summarize_periodicity",
]
