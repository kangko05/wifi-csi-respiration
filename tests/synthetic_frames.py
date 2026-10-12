"""Synthetic CSI frames for tests, no hardware and no packet format needed.

Empty room = fixed channel + noise; breathing = amplitude and phase modulation at
breathing_hz; a random CFO per packet. The fixed channel is smooth across bins and the
middle bin is DC (zero), as the 20 MHz ESP link delivers it.
"""

from __future__ import annotations

import numpy as np

from respiration.frame import Frame

PACKET_RATE_HZ = 92.0
START_T_S = 1.7e9
BIN_COUNT = 57
BREATHING_HZ = 0.25
BREATHING_DEPTH = 0.08
BASE_PHASE_STEP_RAD = 0.3
GAIN_KEY = (30, 4, 8)


def frames(
    seconds: float,
    breathing: bool,
    seed: int,
    frozen_span: tuple[int, int] | None = None,
    gap_after_s: float | None = None,
    gap_s: float = 0.0,
    bin_count: int = BIN_COUNT,
    breathing_hz: float = BREATHING_HZ,
) -> list[Frame]:
    """Frames at PACKET_RATE_HZ for `seconds`.

    frozen_span=(first, last) repeats frame `first`'s CSI up to index `last` (a stuck RX).
    gap_after_s/gap_s shift every later frame by gap_s seconds on both clocks.
    bin_count must be odd; the middle bin is DC (zero).
    """
    rng = np.random.default_rng(seed)

    half = bin_count // 2
    dc_bin = half
    bins = np.arange(-half, half + 1)

    # Smooth across frequency like a real channel: independent random phases per bin
    # would put near-pi steps between neighbours that the phase unwrapping folds
    # inconsistently.
    phase_steps = rng.normal(0, BASE_PHASE_STEP_RAD, bin_count)
    base = 40 * np.exp(1j * (rng.uniform(-np.pi, np.pi) + np.cumsum(phase_steps)))

    out = []
    frozen_csi = None

    for index in range(int(seconds * PACKET_RATE_HZ)):
        t = index / PACKET_RATE_HZ

        if breathing:
            modulation = BREATHING_DEPTH * np.sin(2 * np.pi * breathing_hz * t)
        else:
            modulation = 0.0

        cfo = rng.uniform(-np.pi, np.pi)
        channel = base * (1 + modulation) * np.exp(1j * (modulation * bins / half + cfo))
        channel += rng.normal(0, 1.0, bin_count) + 1j * rng.normal(0, 1.0, bin_count)
        channel[dc_bin] = 0

        # int8 quantisation as the board delivers it
        imag = np.clip(np.rint(channel.imag), -127, 127)
        real = np.clip(np.rint(channel.real), -127, 127)
        csi = (real + 1j * imag).astype(np.complex64)

        if frozen_span is not None and frozen_span[0] <= index <= frozen_span[1]:
            if frozen_csi is None:
                frozen_csi = csi
            csi = frozen_csi

        if gap_after_s is not None and t >= gap_after_s:
            t += gap_s

        out.append(Frame(t_s=START_T_S + t, device_time_us=int(t * 1e6) % 2**32, csi=csi, gain_key=GAIN_KEY))

    return out


def amplitude_frames(t: np.ndarray, amps: np.ndarray) -> list[Frame]:
    """Frames whose |csi| is the given (N, K) amplitude array at times t (for the apnea pipe tests)."""
    out = []

    for t_s, row in zip(t, amps):
        out.append(Frame(t_s=float(t_s), device_time_us=int(t_s * 1e6) % 2**32, csi=row.astype(np.complex64)))

    return out
