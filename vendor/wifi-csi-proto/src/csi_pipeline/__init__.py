"""Modular offline CSI analysis: validated input and explicit gain alternatives."""

from .gain import GainConfig, GainResult, correct_gain
from .input import InputValidationError, SessionData, load_session, summarize_session
from .quality import QualityConfig, QualityResult, WindowQuality, assess_quality

__all__ = [
    "GainConfig", "GainResult", "correct_gain",
    "InputValidationError", "SessionData", "load_session", "summarize_session",
    "QualityConfig", "QualityResult", "WindowQuality", "assess_quality",
]
