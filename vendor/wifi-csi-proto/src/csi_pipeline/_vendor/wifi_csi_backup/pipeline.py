"""Wire the stages together.

Every stage is a pure function, so the same functions can later be wrapped in a
sliding window for real-time use without changing any of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .estimate import quality, rate
from .estimate.rate import RateEstimate
from .features import cir, selection
from .model import CsiBlock
from .preprocess import filters, gain, phase, resample


@dataclass
class Config:
    gain_method: str = "rms_norm"
    phase_method: str = "los_wls"
    fs: float | None = None  # None -> infer from median packet interval
    # Leading CIR taps kept.  Dou & Huan use 3 at 20 MHz; at our 36.6 MHz the
    # delay resolution is 27 ns (8.2 m of path), so a person 1 m away shares
    # tap 0 with the direct leakage -- taps cannot isolate them, they only cut
    # far multipath.  Measured on a real capture: 8 taps beat 3 (pm 11.9 vs
    # 6.5), and 20 adds nothing over 8.
    n_taps: int = 8
    top_columns: int = 5  # taps/subcarriers combined into the waveform
    use_cir: bool = True


@dataclass
class Result:
    estimate: RateEstimate
    fs: float
    waveform: np.ndarray
    block: CsiBlock  # resampled onto the uniform grid
    corrected: CsiBlock  # gain+phase corrected, still on original packets
    timing: dict = field(default_factory=dict)


def run(block: CsiBlock, config: Config | None = None) -> Result:
    cfg = config or Config()

    timing = resample.gap_report(block)

    corrected = gain.correct(block, cfg.gain_method)
    corrected = phase.correct(corrected, cfg.phase_method)

    # Resampling after correction: tau/psi are per-packet properties of the
    # captured frames, so they must be removed from real packets, not from
    # interpolated ones.
    uniform, fs = resample.to_uniform(corrected, cfg.fs)

    if cfg.use_cir:
        columns = cir.dynamic_taps(uniform, cfg.n_taps)
    else:
        columns = cir.remove_static(uniform.H)

    waveform = selection.combine(columns, fs, cfg.top_columns)
    waveform = filters.hampel(waveform)
    waveform = filters.bandpass(waveform, fs)

    est = rate.estimate(cir.remove_static(uniform.H), waveform, fs)

    return Result(
        estimate=est,
        fs=fs,
        waveform=waveform,
        block=uniform,
        corrected=corrected,
        timing=timing,
    )


def compare_methods(
    block: CsiBlock,
    gains=("firmware_agc", "rms_norm"),
    phases=("none", "linear_fit", "los_wls"),
) -> list[dict]:
    """A/B every preprocessing combination on one capture.

    This is how firmware_agc vs rms_norm and los_wls vs the linear-fit baseline
    get settled on real data instead of by argument.
    """
    rows = []
    for g in gains:
        for p in phases:
            try:
                res = run(block, Config(gain_method=g, phase_method=p))
            except Exception as exc:  # noqa: BLE001 - report, don't abort the sweep
                rows.append({"gain": g, "phase": p, "error": str(exc)})
                continue
            rows.append(
                {
                    "gain": g,
                    "phase": p,
                    "bpm": res.estimate.bpm,
                    "spectral_bpm": res.estimate.spectral_bpm,
                    "peak_count_bpm": res.estimate.peak_count_bpm,
                    "pm": res.estimate.pm,
                    "band_snr": res.estimate.detail["band_snr"],
                    "confident": res.estimate.confident,
                }
            )
    return rows
