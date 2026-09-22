"""Synchronous one-window respiration pipeline over the preserved legacy methods.

    import sys; sys.path.insert(0, "src")   # or PYTHONPATH=src from the repo root
    from csi_respiration import RespirationPipeline, PipelineConfig
    result = RespirationPipeline(PipelineConfig()).process(lines)

`lines` is a sequence of complete `CSI_DATA,...` strings (or ASCII bytes) or of
`preprocessing.CSIdata` records. See `docs/python-pipeline.md`.
"""

from ._legacy import GainConfig, PeriodicityConfig, PhaseCirConfig, QualityConfig
from .adapter import (
    SUPPORTED_PROFILE,
    PipelineInputError,
    RecordValidationError,
    TimestampOrderError,
    UnsupportedProfileError,
    build_session,
    normalize_records,
)
from .pipeline import (
    METHODS,
    STATUS_ACCEPTED,
    STATUS_INSUFFICIENT,
    STATUS_UNAVAILABLE,
    STATUS_WITHHELD,
    MethodResult,
    PipelineConfig,
    PipelineResult,
    RespirationPipeline,
)

__all__ = [
    "GainConfig", "METHODS", "MethodResult", "PeriodicityConfig", "PhaseCirConfig",
    "PipelineConfig", "PipelineInputError", "PipelineResult", "QualityConfig",
    "RecordValidationError", "RespirationPipeline", "STATUS_ACCEPTED", "STATUS_INSUFFICIENT",
    "STATUS_UNAVAILABLE", "STATUS_WITHHELD", "SUPPORTED_PROFILE", "TimestampOrderError",
    "UnsupportedProfileError", "build_session", "normalize_records",
]
