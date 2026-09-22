"""One synchronous window: records -> per-method respiration decisions.

Each selected method runs the preserved legacy code unchanged, exactly as
`scripts/run_legacy_compare.py` calls it:

* amplitude: `estimate_periodicity(assess_quality(correct_gain(session),
  QualityConfig(window_s=None)), PeriodicityConfig())`, full-window result.
* phase, cir: one shared `run_phase_cir(session, PhaseCirConfig())`.

Methods never vote and no overall rate is produced. A missing rate is `None`,
never 0. Only the expected data conditions are turned into a status
(`PhaseCirUnavailable`, the amplitude path's empty-window reasons, fewer than
two packets); anything else propagates.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import numpy as np

from preprocessing import CSIdata

from ._legacy import (
    GainConfig,
    PeriodicityConfig,
    PhaseCirConfig,
    PhaseCirUnavailable,
    QualityConfig,
    SessionData,
    assess_quality,
    correct_gain,
    estimate_periodicity,
    run_phase_cir,
    summarize_periodicity,
)
from .adapter import build_session, check_profile, normalize_records

METHODS = ("amplitude", "phase", "cir")

STATUS_ACCEPTED = "accepted"  # candidate passed the method's unchanged gate
STATUS_WITHHELD = "withheld"  # a candidate exists but failed the gate
STATUS_UNAVAILABLE = "unavailable"  # data did not support this method; no candidate
STATUS_INSUFFICIENT = "insufficient_data"  # too few packets / too short to analyse

INSUFFICIENT_REASONS = frozenset(
    {
        "fewer_than_two_packets",
        "too_few_packets",
        "too_few_valid_packets",
        "insufficient_duration",
        "insufficient_duration_after_edge_trim",
        "insufficient_complete_packets",
    }
)


@dataclass(frozen=True)
class PipelineConfig:
    """Baseline legacy settings; nothing here is tuned.

    `quality.window_s` must stay None: one call analyses the whole window it is
    given. `phase_cir.layout_profile` is the backup's assumed HT40 117-bin
    ordering, the only one the vendored code supports.
    """

    methods: tuple[str, ...] = METHODS
    amplitude_gain: GainConfig = field(default_factory=GainConfig)
    quality: QualityConfig = field(default_factory=lambda: QualityConfig(window_s=None))
    periodicity: PeriodicityConfig = field(default_factory=PeriodicityConfig)
    phase_cir: PhaseCirConfig = field(default_factory=PhaseCirConfig)
    session_id: str = "in-memory"

    def __post_init__(self):
        methods = self.methods
        if isinstance(methods, str) or not isinstance(methods, (tuple, list)):
            raise TypeError("methods must be a tuple of method names")

        methods = tuple(methods)
        if not methods:
            raise ValueError("select at least one method")

        unknown = [m for m in methods if m not in METHODS]
        if unknown:
            raise ValueError(f"unknown methods {unknown}; choose from {METHODS}")

        if len(set(methods)) != len(methods):
            raise ValueError("methods must not repeat")

        object.__setattr__(self, "methods", methods)

        for name, kind in (
            ("amplitude_gain", GainConfig),
            ("quality", QualityConfig),
            ("periodicity", PeriodicityConfig),
            ("phase_cir", PhaseCirConfig),
        ):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f"{name} must be {kind.__name__}")

        if self.quality.window_s is not None:
            raise ValueError(
                "quality.window_s must be None; window scheduling is the caller's job"
            )

        if not isinstance(self.session_id, str):
            raise TypeError("session_id must be str")


@dataclass(frozen=True)
class MethodResult:
    """One method's own decision.

    candidate_bpm: the method's diagnostic peak (amplitude: best-scoring bin's
    PSD peak; phase/CIR: backup `diagnostic_bpm`), shown even when withheld.
    accepted_bpm: set only when the unchanged gate accepts (amplitude: highest
    scoring accepted bin, which may differ from the best bin). Neither is a
    presence or apnea decision; `unavailable` is not "no breathing".
    diagnostics: the vendor result object (PeriodicityResult or PhaseCirResult)
    or None when the method could not run.
    """

    method: str
    status: str
    candidate_bpm: float | None
    accepted_bpm: float | None
    reasons: tuple[str, ...]
    elapsed_s: float
    details: Mapping[str, Any]
    diagnostics: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "candidate_bpm": self.candidate_bpm,
            "accepted_bpm": self.accepted_bpm,
            "reasons": list(self.reasons),
            "elapsed_s": self.elapsed_s,
            "details": _jsonable(dict(self.details)),
        }


@dataclass(frozen=True)
class PipelineResult:
    config: PipelineConfig
    input_kind: str | None  # "raw", "csidata" or None for empty input
    records: tuple[CSIdata, ...] = field(repr=False)
    session: SessionData | None = field(repr=False)
    timestamp_policy: Mapping[str, Any] | None
    methods: Mapping[str, MethodResult]
    elapsed_s: float

    @property
    def n_records(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        session = self.session

        return {
            "session_id": self.config.session_id,
            "input_kind": self.input_kind,
            "n_records": self.n_records,
            "window": (
                None
                if session is None
                else {
                    "first_device_timestamp_us": int(
                        session.metadata["local_timestamp"][0]
                    ),
                    "last_device_timestamp_us": int(
                        session.metadata["local_timestamp"][-1]
                    ),
                    "duration_s": float(session.time_s[-1]),
                    "n_valid_packets": int(session.valid_packet_mask.sum()),
                    "n_valid_bins": int(session.valid_bin_mask.sum()),
                    "max_interval_s": float(np.diff(session.time_s).max()),
                }
            ),
            "timestamp_policy": (
                _jsonable(dict(self.timestamp_policy))
                if self.timestamp_policy
                else None
            ),
            "methods": {
                name: result.to_dict() for name, result in self.methods.items()
            },
            "elapsed_s": self.elapsed_s,
            "config": _jsonable(asdict(self.config)),
        }


def _jsonable(value):
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]

    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())

    if isinstance(value, np.generic):
        return _jsonable(value.item())

    if isinstance(value, float) and not np.isfinite(value):
        return None

    return value


def _positive(value) -> float | None:
    return (
        float(value) if value is not None and np.isfinite(value) and value > 0 else None
    )


def _no_candidate_status(reasons) -> str:
    return (
        STATUS_INSUFFICIENT
        if INSUFFICIENT_REASONS.intersection(reasons)
        else STATUS_UNAVAILABLE
    )


class RespirationPipeline:
    """`RespirationPipeline(config).process(records)` -> `PipelineResult`.

    Stateless between calls; no ports, files or threads are opened.
    """

    def __init__(self, config: PipelineConfig | None = None):
        config = PipelineConfig() if config is None else config
        if not isinstance(config, PipelineConfig):
            raise TypeError("config must be PipelineConfig")

        self.config = config

    def process(self, records: Iterable[str | bytes | CSIdata]) -> PipelineResult:
        started = time.perf_counter()
        normalized, kind = normalize_records(records)
        if normalized:  # an unsupported profile is an error even for one packet
            check_profile(normalized)

        if len(normalized) < 2:
            reasons = ("fewer_than_two_packets",)
            methods = {
                name: MethodResult(
                    name,
                    STATUS_INSUFFICIENT,
                    None,
                    None,
                    reasons,
                    0.0,
                    MappingProxyType({"n_records": len(normalized)}),
                )
                for name in self.config.methods
            }

            return PipelineResult(
                self.config,
                kind,
                normalized,
                None,
                None,
                MappingProxyType(methods),
                time.perf_counter() - started,
            )

        session, policy = build_session(
            normalized, self.config.session_id, input_kind=kind
        )

        results: dict[str, MethodResult] = {}
        if "amplitude" in self.config.methods:
            results["amplitude"] = self._amplitude(session)

        requested = [m for m in self.config.methods if m != "amplitude"]
        if requested:
            results.update(self._phase_cir(session, requested))

        ordered = {name: results[name] for name in self.config.methods}

        return PipelineResult(
            self.config,
            kind,
            normalized,
            session,
            MappingProxyType(policy),
            MappingProxyType(ordered),
            time.perf_counter() - started,
        )

    def _amplitude(self, session: SessionData) -> MethodResult:
        started = time.perf_counter()
        quality = assess_quality(
            correct_gain(session, self.config.amplitude_gain), self.config.quality
        )
        result = estimate_periodicity(quality, self.config.periodicity)
        elapsed = time.perf_counter() - started

        full = result.full_session
        best, selected = full.best, full.selected
        candidate = _positive(best.psd_hz * 60) if best else None
        accepted = _positive(selected.psd_hz * 60) if selected else None

        if selected is not None:
            status, reasons = STATUS_ACCEPTED, tuple(selected.reasons)
        elif candidate is not None:
            status, reasons = STATUS_WITHHELD, tuple(best.reasons)
        else:
            reasons = tuple(full.reasons) + tuple(full.window.blocking_reasons)
            if best is None and full.candidates:
                reasons += ("all_processed_bins_flat",)
            status = _no_candidate_status(reasons)

        window = full.window
        details = {
            "gain_method": self.config.amplitude_gain.method,
            "best_bin": best.bin_index if best else None,
            "selected_bin": selected.bin_index if selected else None,
            "best_concentration": best.concentration if best else None,
            "best_acf_peak": best.acf_peak if best else None,
            "best_acf_bpm": best.acf_hz * 60 if best and best.acf_hz else None,
            "analysis_start_s": float(full.time_s[0]) if len(full.time_s) else None,
            "analysis_end_s": float(full.time_s[-1]) if len(full.time_s) else None,
            "resolution_bpm": full.resolution_hz * 60 if full.resolution_hz else None,
            "n_processed_bins": len(full.candidates),
            "n_supported_bins": sum(
                c.status == "periodic_candidate" for c in full.candidates
            ),
            "quality_data_usable": window.data_usable,
            "quality_blocking_reasons": list(window.blocking_reasons),
            "quality_warnings": list(window.warnings),
            "quality_metrics": dict(window.metrics),
            "vendor_summary": summarize_periodicity(full),
        }

        return MethodResult(
            "amplitude",
            status,
            candidate,
            accepted,
            reasons,
            elapsed,
            MappingProxyType(details),
            result,
        )

    def _phase_cir(
        self, session: SessionData, requested: list[str]
    ) -> dict[str, MethodResult]:
        started = time.perf_counter()
        try:
            result = run_phase_cir(session, self.config.phase_cir)
        except PhaseCirUnavailable as exc:
            elapsed = time.perf_counter() - started
            reasons = (str(exc),)

            details = MappingProxyType(
                {
                    "layout_profile": self.config.phase_cir.layout_profile,
                    "shared_phase_cir_run": True,
                }
            )

            return {
                m: MethodResult(
                    m,
                    _no_candidate_status(reasons),
                    None,
                    None,
                    reasons,
                    elapsed,
                    details,
                )
                for m in requested
            }

        elapsed = time.perf_counter() - started
        support = result.support
        out = {}

        for method in requested:
            decision = getattr(result, method)
            candidate = _positive(decision.diagnostic_bpm)
            accepted = _positive(decision.accepted_bpm) if decision.accepted else None
            reasons = tuple(decision.reasons)

            if accepted is not None:
                status = STATUS_ACCEPTED
            elif candidate is not None:
                status = STATUS_WITHHELD
            else:
                reasons = reasons or ("no_candidate",)
                status = _no_candidate_status(reasons)

            details = {
                "spectral_bpm": decision.spectral_bpm,
                "peak_count_bpm": decision.peak_count_bpm,
                "estimate_method": decision.estimate_method,
                "sharpness": decision.sharpness,
                "agreement_bpm": decision.agreement_bpm,
                "peak_to_median": decision.peak_to_median,
                "band_snr": decision.band_snr,
                "n_peaks": decision.n_peaks,
                "selected_feature_indices": decision.selected_feature_indices.tolist(),
                "feature_kind": decision.feature_kind,
                "layout_profile": support.layout_profile,
                "n_used_packets": int(len(support.original_row_indices)),
                "n_used_bins": int(len(support.original_bin_indices)),
                "analysis_start_s": float(support.time_s[0]),
                "analysis_end_s": float(support.time_s[-1]),
                "resolution_bpm": support.resolution_hz * 60,
                "sample_rate_hz": support.sample_rate_hz,
                "max_retained_gap_s": support.max_retained_gap_s,
                "shared_phase_cir_run": True,
            }

            out[method] = MethodResult(
                method,
                status,
                candidate,
                accepted,
                reasons,
                elapsed,
                MappingProxyType(details),
                result,
            )

        return out
