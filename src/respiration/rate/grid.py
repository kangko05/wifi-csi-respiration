"""One window of packets -> a sanitised 10 Hz complex grid, or a reason to hold.

Steps (the colleague's single-antenna ESP32 preprocessing, kept unchanged):
  1. hold checks on the raw packets: a frozen receiver (long run of identical CSI),
     too many lost packets (fill ratio), guard/DC bins dropped
  2. sanitize: per packet remove the common phase (CFO) and the linear phase slope
     over subcarriers (STO); level the gain per run of equal gain key
  3. uniform: box-average onto a 10 Hz grid on the device clock, cut at gaps longer
     than MAX_GAP_S and keep the longest continuous run; hold when it covers less than
     MIN_COVER of the window
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from respiration.frame import Frame

RATE_HZ = 10.0  # grid rate; more than 10x the 0.6 Hz breathing ceiling
MAX_GAP_S = 1.0  # a gap longer than this is not bridged; the grid is cut there
MIN_COVER = 0.83  # the longest continuous run must cover this share of the window

FROZEN_RUN = 185  # this many identical consecutive packets = the receiver repeated a stale buffer
MIN_FILL = 0.85  # received / expected packets below this = too lossy to judge
LIVE_BIN_SHARE = 0.99  # a bin is live when non-zero in more than this share of packets

DEVICE_CLOCK_WRAP = 2**32

WINDOW_OK = "ok"
HOLD_FROZEN = "frozen"
HOLD_LOSSY = "lossy"
HOLD_SHORT = "short"


def longest_identical_run(csi: np.ndarray) -> int:
    """Longest run of bit-identical consecutive packets (0 when no two neighbours match)."""
    same_as_previous = np.all(csi[1:] == csi[:-1], axis=1)

    best = 0
    run = 0

    for is_same in same_as_previous:
        if is_same:
            run += 1
        else:
            run = 0

        best = max(best, run)

    if best == 0:
        return 0

    return best + 1


def fill_ratio(device_time_us: np.ndarray) -> float:
    """Received packets / expected packets; expected = device span / median packet gap.

    The median gap is robust to bursty loss, so bursts lower the ratio as they should.
    """
    gaps = np.diff(np.asarray(device_time_us, dtype=np.int64)) % DEVICE_CLOCK_WRAP

    if len(gaps) == 0:
        return 0.0

    median_gap = max(float(np.median(gaps)), 1.0)
    expected = max(float(gaps.sum()) / median_gap + 1, 1.0)

    return len(device_time_us) / expected


def stack_frames(
    frames: Sequence[Frame],
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Frames in arrival order -> (csi (N, K) complex64, device_time_us (N,) int64, gain_key (N, G) or None).

    gain_key is None when any frame has none.
    """
    csi = np.stack([frame.csi for frame in frames]).astype(np.complex64)
    device_time_us = np.array(
        [frame.device_time_us for frame in frames], dtype=np.int64
    )

    gain_key = None
    if all(frame.gain_key is not None for frame in frames):
        gain_key = np.array([frame.gain_key for frame in frames], dtype=np.int64)

    return csi, device_time_us, gain_key


def live_bins(csi: np.ndarray, device_time_us: np.ndarray, gain_key: np.ndarray | None):
    """Drop guard/DC bins and the packets with a zero in any remaining bin.

    Returns (csi of live bins, device time, live bin indices, gain key) of the good packets.
    """
    is_nonzero = np.abs(csi) > 0
    live = np.mean(is_nonzero, axis=0) > LIVE_BIN_SHARE
    good = np.all(is_nonzero[:, live], axis=1)
    coords = np.flatnonzero(live)

    if gain_key is not None:
        gain_key = gain_key[good]

    return csi[good][:, coords], device_time_us[good], coords, gain_key


def _continuous_phase(csi: np.ndarray, coords: np.ndarray) -> np.ndarray:
    # np.unwrap goes off by 2 pi where a missing bin (the DC) doubles the step. Take the
    # slope first from the pairs one bin apart (complex mean), then fold every step
    # towards that slope.
    gaps = np.diff(coords)
    ratio = csi[:, 1:] * np.conj(csi[:, :-1])
    unit = gaps == 1

    if unit.any():
        coarse = np.angle(ratio[:, unit].sum(axis=1))
    else:
        coarse = np.zeros(len(csi))

    step = np.angle(ratio)
    step += 2 * np.pi * np.round((coarse[:, None] * gaps - step) / (2 * np.pi))
    start = np.angle(csi[:, :1])

    return np.concatenate([start, start + np.cumsum(step, axis=1)], axis=1)


def segment_median(values: np.ndarray, segment: np.ndarray) -> np.ndarray:
    """Median of each continuous segment (segment ids 0, 1, 2, ... non-decreasing), spread back over its members.

    The gain key changes often (about 1,900 segments in a 120 s window); one np.median
    per segment took 0.3 s on the board, so one sort handles every segment at once.
    """
    order = np.lexsort((values, segment))
    sorted_values = values[order]
    sorted_segments = segment[order]

    start = np.flatnonzero(
        np.concatenate([[True], sorted_segments[1:] != sorted_segments[:-1]])
    )
    count = np.diff(np.concatenate([start, [len(sorted_segments)]]))
    median = (
        sorted_values[start + (count - 1) // 2] + sorted_values[start + count // 2]
    ) / 2

    return median[np.searchsorted(sorted_segments[start], segment)]


def sanitize(
    csi: np.ndarray, coords: np.ndarray, gain_key: np.ndarray | None = None
) -> np.ndarray:
    """Complex CSI with CFO (common phase) and STO (linear slope over bins) removed and the gain levelled.

    Dividing every packet by its own mean amplitude would also remove the breathing
    amplitude change common to all bins, so the gain is one median per run of equal
    gain key (AGC, FFT gain, TX power) instead. The common phase cannot be told from
    CFO with one antenna, so it has to go.
    """
    csi = np.asarray(csi, dtype=np.complex128)
    coords = np.asarray(coords, dtype=float)

    amplitude = np.abs(csi)
    gain = amplitude.mean(axis=1, keepdims=True)

    if gain_key is not None:
        key = np.asarray(gain_key).reshape(len(gain_key), -1)
        segment = np.concatenate(
            [[0], np.cumsum(np.any(np.diff(key, axis=0) != 0, axis=1))]
        )
        gain[:, 0] = segment_median(gain[:, 0], segment)

    phase = _continuous_phase(csi, coords)
    centred = coords - coords.mean()
    phase -= (phase @ centred / (centred @ centred))[:, None] * centred
    phase -= phase.mean(axis=1, keepdims=True)

    return amplitude / np.where(gain > 0, gain, 1) * np.exp(1j * phase)


def uniform(
    z: np.ndarray,
    t_us: np.ndarray,
    rate_hz: float = RATE_HZ,
    max_gap_s: float = MAX_GAP_S,
) -> tuple[np.ndarray, float]:
    """Move onto an even grid on the device clock; cut at gaps over max_gap_s and keep the longest run.

    ~92 Hz -> 10 Hz is a box average (no aliasing); empty cells are linearly interpolated.
    Returns (grid (T, K) complex, span of the run in seconds).
    """
    t = np.asarray(t_us, dtype=np.int64)
    t = (
        np.cumsum(np.concatenate([[0], np.diff(t) % DEVICE_CLOCK_WRAP])) / 1e6
    )  # unwrap the uint32 clock

    cut = np.flatnonzero(np.diff(t) > max_gap_s)
    bounds = np.concatenate([[0], cut + 1, [len(t)]])
    i0, i1 = max(zip(bounds[:-1], bounds[1:]), key=lambda b: t[b[1] - 1] - t[b[0]])
    t = t[i0:i1]
    z = z[i0:i1]

    if len(t) < 2:
        return np.empty((0, z.shape[1]), complex), 0.0

    edges = np.arange(t[0], t[-1] + 1 / rate_hz, 1 / rate_hz)
    index = np.clip(np.searchsorted(edges, t, side="right") - 1, 0, len(edges) - 1)
    count = np.bincount(index, minlength=len(edges)).astype(float)

    out = np.empty((len(edges), z.shape[1]), complex)
    for k in range(z.shape[1]):
        total = np.bincount(index, z[:, k].real, len(edges)) + 1j * np.bincount(
            index, z[:, k].imag, len(edges)
        )
        out[:, k] = total / np.where(count > 0, count, 1)

    have = count > 0
    if not have.all():
        grid = np.arange(len(edges))
        columns = []
        for k in range(out.shape[1]):
            real = np.interp(grid, grid[have], out[have, k].real)
            imag = np.interp(grid, grid[have], out[have, k].imag)
            columns.append(real + 1j * imag)
        out = np.stack(columns, axis=1)

    return out, float(t[-1] - t[0])


def window_grid(csi, t_us, coords, gain_key, window_s: float) -> np.ndarray | None:
    """Sanitised, resampled (T, K) grid of one window; None when the run covers under MIN_COVER of it."""
    z, span = uniform(sanitize(csi, coords, gain_key), t_us)

    if span < window_s * MIN_COVER:
        return None

    return z


def sanitised_grid(
    frames: Sequence[Frame], window_s: float
) -> tuple[str, np.ndarray | None]:
    """One window of frames -> (WINDOW_OK, grid) or (hold reason, None). Hold reasons in order: frozen, lossy, short."""
    if len(frames) < 2:
        return HOLD_SHORT, None

    csi, device_time_us, gain_key = stack_frames(frames)

    if longest_identical_run(csi) >= FROZEN_RUN:
        return HOLD_FROZEN, None

    if fill_ratio(device_time_us) < MIN_FILL:
        return HOLD_LOSSY, None

    csi, device_time_us, coords, gain_key = live_bins(csi, device_time_us, gain_key)
    grid = window_grid(csi, device_time_us, coords, gain_key, window_s)

    if grid is None:
        return HOLD_SHORT, None

    return WINDOW_OK, grid
