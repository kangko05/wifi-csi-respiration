"""Raw CSI byte buffer -> complex subcarriers on a known frequency grid.

Getting this wrong silently breaks the timing-offset estimate downstream, which
reads tau off the phase slope against frequency.  So the mapping is asserted
against a real capture (see :func:`validate_layout`) rather than assumed.

Measured on ESP32-C5 / IDF 6.0.3, channel 36, HT40, rx_format 2: the callback
hands back 234 bytes = 117 complex bins, already in **ascending signed
subcarrier order** from -58 to +58 -- not FFT order, and not the full 128-bin
FFT.  Bins -1, 0, +1 read exactly zero, which is what pins the mapping down.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SUBCARRIER_SPACING = 312_500.0  # Hz, 802.11n

# Each plan: half-span of the occupied band, the nulled indices inside it, and
# the pilot indices.  n_bins is 2*span+1 because the hardware includes the
# nulls in the buffer.
_PLANS = {
    # Confirmed by measurement (see module docstring).
    "HT40": dict(span=58, nulls=(-1, 0, 1), pilots=(11, 25, 53), measured=True),
    # Same structure, inferred from the 802.11n HT20 plan; re-check the null
    # positions with validate_layout on the first HT20 capture.
    "HT20": dict(span=28, nulls=(0,), pilots=(7, 21), measured=False),
}


@dataclass(frozen=True)
class Layout:
    name: str
    n_bins: int  # bins as delivered by the hardware, nulls included
    index: np.ndarray  # [K] signed subcarrier index of the usable bins
    take: np.ndarray  # [K] position of each usable bin in the raw buffer
    data_mask: np.ndarray  # [K] bool, False on pilots
    f: np.ndarray  # [K] Hz offset from carrier
    null_positions: np.ndarray  # buffer positions that must read zero
    measured: bool  # False = plan inferred, not yet confirmed on hardware

    @property
    def n(self) -> int:
        return self.index.size


def _build(name: str) -> Layout:
    plan = _PLANS[name]
    span, nulls, pilots = plan["span"], plan["nulls"], plan["pilots"]

    raw_index = np.arange(-span, span + 1)  # what each buffer position means
    usable = ~np.isin(raw_index, nulls)

    index = raw_index[usable]
    return Layout(
        name=name,
        n_bins=raw_index.size,
        index=index,
        take=np.flatnonzero(usable),
        data_mask=~np.isin(np.abs(index), pilots),
        f=index * SUBCARRIER_SPACING,
        null_positions=np.flatnonzero(~usable),
        measured=plan["measured"],
    )


HT20 = _build("HT20")
HT40 = _build("HT40")
LAYOUTS = (HT20, HT40)


def bytes_to_complex(buf: np.ndarray, first_word_invalid: bool = False) -> np.ndarray:
    """Decode one packet's int8 buffer into complex bins, ascending in k.

    ESP32 stores each bin as an (imaginary, real) int8 pair.  When
    ``first_word_invalid`` is set the leading four bytes (two bins) are garbage
    from a hardware limitation and are zeroed rather than dropped, so the bin
    indexing stays aligned.
    """
    b = np.asarray(buf, dtype=np.int8)
    if b.size % 2:
        b = b[:-1]
    out = b[1::2].astype(np.complex128) + 1j * b[0::2].astype(np.complex128)
    if first_word_invalid and out.size >= 2:
        out = out.copy()
        out[:2] = 0
    return out


def infer_layout(n_bins: int) -> Layout:
    for layout in LAYOUTS:
        if n_bins == layout.n_bins:
            return layout
    raise ValueError(
        f"{n_bins} CSI bins matches no known layout "
        f"({[(l.name, l.n_bins) for l in LAYOUTS]}). Dump the mean magnitude "
        f"per bin and locate the zero bins to work out the mapping."
    )


def extract(bins: np.ndarray, layout: Layout) -> np.ndarray:
    """Drop the null bins, leaving the usable subcarriers ascending in k."""
    return bins[..., layout.take]


def validate_layout(bins: np.ndarray, layout: Layout,
                    ratio: float = 0.05) -> dict:
    """Check that the bins that should be null really are, on real data.

    Returns the measured magnitudes.  Raises if the null bins carry energy,
    which means the byte order or the subcarrier plan is wrong -- and nothing
    downstream would tell you, it would just estimate tau off a wrong slope.
    """
    mag = np.abs(bins).mean(axis=0)
    if mag.size != layout.n_bins:
        raise AssertionError(
            f"got {mag.size} bins but layout {layout.name} expects {layout.n_bins}"
        )

    null_mag = float(mag[layout.null_positions].mean())
    occ_mag = float(mag[layout.take].mean())

    report = {
        "layout": layout.name,
        "occupied_mean": occ_mag,
        "null_mean": null_mag,
        "ratio": null_mag / occ_mag if occ_mag else float("inf"),
        "measured_plan": layout.measured,
    }
    if occ_mag <= 0:
        raise AssertionError(f"occupied subcarriers are all zero: {report}")
    if report["ratio"] > ratio:
        raise AssertionError(
            f"bins {layout.null_positions.tolist()} should be null but carry "
            f"{report['ratio']:.3f} of the occupied magnitude; the "
            f"{layout.name} subcarrier mapping or byte order is wrong: {report}"
        )
    return report


def magnitude_profile(bins: np.ndarray) -> np.ndarray:
    """Mean |H| per raw bin -- the tool for working out an unknown mapping."""
    return np.abs(bins).mean(axis=0)
