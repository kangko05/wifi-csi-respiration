"""Timing-offset (tau) and common-phase-error (psi) correction.

Both errors are i.i.d. per packet on real hardware -- verified in Ratnam et al.
(IEEE TWC 2024) Fig. 5 on Intel AX210 and BCM4358, uncorrelated for packet
intervals >= 50 ms.  A single-antenna radio like the ESP32-C5 cannot cancel
them with an antenna-pair ratio, so they must be estimated per packet and
divided out, or the CSI phase is unusable.

Corrected CSI, paper eq (3):  h_hat[p,k] = h_bar[p,k] * exp(j*2*pi*f_k*tau_p) * exp(j*psi_p)

Reference: Ratnam et al., Section IV.
"""

from __future__ import annotations

import numpy as np

from ..model import CsiBlock

# OFDM symbol duration, 802.11n.
T_SYMBOL = 3.2e-6


def _apply(H: np.ndarray, f: np.ndarray, tau: np.ndarray,
           psi: np.ndarray) -> np.ndarray:
    return H * np.exp(1j * (2 * np.pi * np.outer(tau, f) + psi[:, None]))


def coarse(H: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ratnam eq (22), the 802.11az estimator. Used as the initial guess.

    tau from the phase of the lag-1 subcarrier correlation, then psi from the
    residual after de-rotating by tau.
    """
    corr = np.sum(H[:, :-1] * np.conj(H[:, 1:]), axis=1)
    tau = (T_SYMBOL / (2 * np.pi)) * np.angle(corr)
    psi = -np.angle(np.sum(H * np.exp(2j * np.pi * np.outer(tau, f)), axis=1))
    return tau, psi


def static_component(H: np.ndarray, f: np.ndarray, tau: np.ndarray,
                     psi: np.ndarray) -> np.ndarray:
    """Ratnam eq (23): b_k averaged over all P frames, so far less noisy."""
    return np.mean(_apply(H, f, tau, psi), axis=0)


def robust_unwrap(w: np.ndarray, span: int = 3) -> np.ndarray:
    """Ratnam eq (26): unwrap each subcarrier against its local neighbourhood.

    Plain np.unwrap follows noise across a deep fade and drags a 2*pi error
    through the rest of the band; referencing the sum over +-span neighbours
    does not.
    """
    n = w.shape[-1]
    ref = np.empty(w.shape, dtype=complex)
    for k in range(n):
        lo, hi = max(0, k - span), min(n, k + span + 1)
        ref[..., k] = w[..., lo:hi].sum(axis=-1)
    ref_ang = np.unwrap(np.angle(ref), axis=-1)
    return np.mod(np.angle(w) - ref_ang + np.pi, 2 * np.pi) - np.pi + ref_ang


def _weighted_fit(phase: np.ndarray, f: np.ndarray,
                  weight: np.ndarray) -> tuple[float, float]:
    """Closed-form weighted least squares of phase ~ 2*pi*f*tau + psi."""
    x = 2 * np.pi * f
    sw = weight.sum()
    if sw <= 0:
        return 0.0, 0.0
    mx = (weight * x).sum() / sw
    my = (weight * phase).sum() / sw
    var = (weight * (x - mx) ** 2).sum()
    if var <= 0:
        return 0.0, float(my)
    slope = (weight * (x - mx) * (phase - my)).sum() / var
    return float(slope), float(my - slope * mx)


def _wls_pass(H: np.ndarray, f: np.ndarray, target: np.ndarray,
              tau_bar: np.ndarray, keep: np.ndarray
              ) -> tuple[np.ndarray, np.ndarray]:
    """Shared inner loop of eq (25) and eq (28).

    `target` is b_k for Algorithm 4, or the running sum of corrected frames for
    Algorithm 5; the estimator is otherwise identical.
    """
    p = H.shape[0]
    tau = np.zeros(p)
    psi = np.zeros(p)
    fk = f[keep]
    for i in range(p):
        w = np.conj(H[i, keep]) * target[i][keep] if target.ndim == 2 \
            else np.conj(H[i, keep]) * target[keep]
        w = w * np.exp(-2j * np.pi * fk * tau_bar[i])
        unwrapped = robust_unwrap(w)
        slope, intercept = _weighted_fit(unwrapped, fk, np.abs(w))
        tau[i] = tau_bar[i] + slope
        psi[i] = intercept
    return tau, psi


def _keep_mask(b: np.ndarray, threshold: float = 0.1) -> np.ndarray:
    """Paper's subcarrier set: |b_k|^2 > 0.1, normalised to the mean power."""
    power = np.abs(b) ** 2
    scale = power.mean()
    keep = power > threshold * scale if scale > 0 else np.ones(b.size, bool)
    return keep if keep.sum() >= 8 else np.ones(b.size, bool)


def linear_fit(H: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Baseline (21): plain unweighted line fit to the unwrapped phase.

    This is the familiar SpotFi-style sanitisation.  The paper's Remark 2 shows
    it is eq (25) with |b_k| forced to 1 and tau_bar to 0 -- kept only so the
    A/B table has the common practice in it.
    """
    p = H.shape[0]
    tau = np.zeros(p)
    psi = np.zeros(p)
    for i in range(p):
        unwrapped = np.unwrap(np.angle(H[i]))
        slope, intercept = _weighted_fit(unwrapped, f, np.ones(f.size))
        tau[i], psi[i] = -slope, -intercept
    return tau, psi


def los_wls(H: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ratnam Algorithm 4 / eq (25). The default.

    Assumes a strong, roughly frequency-flat LoS path, which holds for two
    boards facing each other.
    """
    tau_bar, psi_bar = coarse(H, f)
    b = static_component(H, f, tau_bar, psi_bar)
    return _wls_pass(H, f, b, tau_bar, _keep_mask(b))


def forward_pass(H: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ratnam Algorithm 5 / eq (28): correlate each frame against its history.

    Drops the frequency-flat assumption.  The first P/10 frames have too little
    history, so Algorithm 4 handles them.
    """
    p = H.shape[0]
    tau_bar, psi_bar = coarse(H, f)
    b = static_component(H, f, tau_bar, psi_bar)
    keep = _keep_mask(b)
    fk = f[keep]

    tau, psi = los_wls(H, f)
    warmup = max(1, p // 10)

    running = _apply(H[:warmup], f, tau[:warmup], psi[:warmup]).sum(axis=0)
    for i in range(warmup, p):
        w = np.conj(H[i, keep]) * running[keep] * np.exp(-2j * np.pi * fk * tau_bar[i])
        slope, intercept = _weighted_fit(robust_unwrap(w), fk, np.abs(w))
        tau[i] = tau_bar[i] + slope
        psi[i] = intercept
        running = running + _apply(H[i:i + 1], f, tau[i:i + 1], psi[i:i + 1])[0]
    return tau, psi


def backward_pass(H: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ratnam Algorithm 6: refine the forward pass using *future* frames.

    Algorithm 5 estimates frame p from its history only, so the earliest frames
    have almost none to work with.  This walks back over the first half using
    the frames after it as the reference instead.
    """
    p = H.shape[0]
    tau, psi = forward_pass(H, f)
    tau_bar, _ = coarse(H, f)
    keep = _keep_mask(static_component(H, f, *coarse(H, f)))
    fk = f[keep]

    # Reference is the corrected second half, which the forward pass got right.
    half = p // 2
    future = _apply(H[half:], f, tau[half:], psi[half:]).sum(axis=0)

    for i in range(half - 1, -1, -1):
        w = np.conj(H[i, keep]) * future[keep] * np.exp(-2j * np.pi * fk * tau_bar[i])
        slope, intercept = _weighted_fit(robust_unwrap(w), fk, np.abs(w))
        tau[i] = tau_bar[i] + slope
        psi[i] = intercept
        future = future + _apply(H[i:i + 1], f, tau[i:i + 1], psi[i:i + 1])[0]
    return tau, psi


METHODS = {
    "none": lambda H, f: (np.zeros(H.shape[0]), np.zeros(H.shape[0])),
    "coarse": coarse,
    "linear_fit": linear_fit,
    "los_wls": los_wls,
    "forward_pass": forward_pass,
    "backward_pass": backward_pass,
}


def correct(block: CsiBlock, method: str = "los_wls") -> CsiBlock:
    if method not in METHODS:
        raise ValueError(f"unknown phase method {method!r}; have {sorted(METHODS)}")
    tau, psi = METHODS[method](block.H, block.f)
    return block.with_H(_apply(block.H, block.f, tau, psi))
