"""Realtime preprocessing: one CSI packet in, breathing waveform samples out.

Three small stages, chained by SlidingWaveform. Every stage uses the past only, so the
waveform a live run produces is the same one an offline pass over the recording gives.

  packet (time, subcarrier amplitudes)
    |  Resampler       resample onto an even grid (FS). A grid sample is the linear
    |                  interpolation between the packets around it, so it is only
    |                  known once the next packet arrives (at most one packet late)
    v
  grid sample (time, amplitudes)
    |  CausalBandpass  keep the breathing band only, with a one-directional filter.
    |                  The first INIT_S median is used as the filter's start level so
    |                  switch-on transients do not look like a breath
    v
  (time, raw amplitudes, filtered amplitudes)
    |  Combiner        collapse the subcarriers into one line. Every HOP_S the per-
    |                  subcarrier scale (last SCALE_S) and the first principal
    |                  component (last WINDOW_S, the direction on which the
    |                  subcarriers move together) are refitted and used for the next hop
    v
  breathing waveform sample (time, value)

Output starts once the first WINDOW_S has passed.
"""

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from respiration.frame import Frame

FS = 20.0                 # resampling rate [Hz]
BAND = (0.1, 0.6)         # breathing band [Hz] (6 to 36 breaths per minute)
WINDOW_S = 30.0           # principal-component window; also when output starts
HOP_S = 1.0               # refit interval for scale and direction
SCALE_S = 120.0           # window for the per-subcarrier scale (MAD)
FILTER_ORDER = 2          # Butterworth order
INIT_S = 1.0              # initial span whose median sets the filter start level


class Resampler:
    """Packet times are irregular; resample onto an even grid (FS) with linear interpolation."""

    def __init__(self, fs: float = FS):
        self.fs = fs
        self.t0 = None          # grid origin = time of the first packet
        self.dt = None          # grid spacing, see push
        self.next_index = 0     # index of the next grid sample to produce
        self.prev_t = None      # time of the previous packet
        self.prev_amps = None   # amplitudes of the previous packet

    def push(self, frame: Frame) -> list[tuple[float, np.ndarray]]:
        """Take one frame; return the grid samples (time, amplitudes) that are now determined.

        Only |csi| and t_s of the frame are used; the frame itself is not kept.
        """
        t = frame.t_s
        amps = np.abs(frame.csi).astype(np.float64)

        if self.prev_t is None:
            self.t0 = t
            # Same spacing np.arange(t0, ..., 1/fs) would use. Wall-clock times are large
            # (about 1.7e9 s), so in floating point the spacing differs very slightly from
            # 1/fs; computing it this way keeps live and offline sample times identical.
            self.dt = (t + 1 / self.fs) - t
            self.prev_t = t
            self.prev_amps = amps

            return []

        out = []

        while True:
            grid_t = self.t0 + self.next_index * self.dt

            if grid_t >= t:
                break  # this grid time needs the next packet

            if grid_t >= self.prev_t:
                out.append((grid_t, self._interpolate(grid_t, t, amps)))

            self.next_index += 1

        # Packets with equal times: keep the earlier one (as np.interp does).
        if t > self.prev_t:
            self.prev_t = t
            self.prev_amps = amps

        return out

    def _interpolate(self, grid_t: float, t: float, amps: np.ndarray) -> np.ndarray:
        ratio = (grid_t - self.prev_t) / (t - self.prev_t)

        return self.prev_amps + ratio * (amps - self.prev_amps)


class CausalBandpass:
    """Breathing band-pass, applied in one direction so only the past is used.

    The first init_s of samples are held back; their median becomes the filter's start
    state, as if the signal had always been at that level. The first packets of a
    recording are often far off, which is why the median is used and not the first sample.
    """

    def __init__(self, fs: float = FS, band: tuple[float, float] = BAND, order: int = FILTER_ORDER,
                 init_s: float = INIT_S):
        self.sos = butter(order, band, btype="bandpass", fs=fs, output="sos")
        self.n_init = int(init_s * fs)
        self.pending = []       # (time, amplitudes) held back until the start state is set
        self.state = None       # filter state (sosfilt zi)

    def push(self, t: float, a: np.ndarray) -> list[tuple[float, np.ndarray, np.ndarray]]:
        """Take one grid sample; return (time, raw, filtered) samples.

        Nothing comes out until the initial span is full; then it all comes out at once.
        """
        if self.state is not None:
            return [(t, a, self._filter_one(a))]

        self.pending.append((t, a))

        if len(self.pending) < self.n_init:
            return []

        start_level = np.median([amps for _, amps in self.pending], axis=0)
        self.state = sosfilt_zi(self.sos)[:, :, None] * start_level

        queued = self.pending
        self.pending = []

        return [(tq, aq, self._filter_one(aq)) for tq, aq in queued]

    def _filter_one(self, a: np.ndarray) -> np.ndarray:
        filtered, self.state = sosfilt(self.sos, a[None, :], axis=0, zi=self.state)

        return filtered[0]


class Combiner:
    """Collapse the subcarrier lines into one breathing waveform.

    Refitted every hop_s:
      scale      per subcarrier, the median absolute deviation (x 1.4826, about one
                 standard deviation) over the last scale_s. Dividing by it evens the
                 subcarriers out. A within-window standard deviation would inflate the
                 noise when the window is a breath hold and squash the breathing after
                 a large jump.
      direction  first principal component of the scaled last window_s. Its sign is
                 kept consistent with the previous direction.
    Columns whose raw amplitude is constant within the window, or whose scale is
    absurdly small next to the others, are dropped (their scale is set to infinity).
    """

    def __init__(self, fs: float = FS, window_s: float = WINDOW_S, hop_s: float = HOP_S, scale_s: float = SCALE_S):
        self.n_window = int(window_s * fs)
        self.n_hop = int(hop_s * fs)
        self.n_scale = max(self.n_window, int(scale_s * fs))
        self.count = 0              # samples received so far
        self.raw_history = []       # raw amplitudes (to find constant columns)
        self.filtered_history = []  # filtered amplitudes
        self.scale = None           # per-subcarrier scale
        self.direction = None       # first principal component

    def push(self, t: float, raw: np.ndarray, filtered: np.ndarray) -> list[tuple[float, float]]:
        """Take one sample; after the first window_s, return one (time, waveform value)."""
        out = []

        if self.count >= self.n_window:
            if (self.count - self.n_window) % self.n_hop == 0:
                self._refit()

            out.append((t, float((filtered / self.scale) @ self.direction)))

        self.count += 1
        self.raw_history.append(raw)
        self.filtered_history.append(filtered)
        self._trim_history()

        return out

    def _refit(self) -> None:
        filtered = np.array(self.filtered_history[-self.n_scale:])
        raw = np.array(self.raw_history[-self.n_window:])

        scale = 1.4826 * np.median(np.abs(filtered - np.median(filtered, axis=0)), axis=0)
        scale[self._useless_columns(raw, scale)] = np.inf

        _, _, vt = np.linalg.svd(filtered[-self.n_window:] / scale, full_matrices=False)
        direction = vt[0]

        if self.direction is not None and direction @ self.direction < 0:
            direction = -direction

        self.scale = scale
        self.direction = direction

    @staticmethod
    def _useless_columns(raw: np.ndarray, scale: np.ndarray) -> np.ndarray:
        """Columns constant within the window (e.g. the zeroed DC bin), or with scale below 1e-6 x the others."""
        constant = np.ptp(raw, axis=0) == 0

        if np.any(scale > 0):
            tiny = scale <= 1e-6 * np.median(scale[scale > 0])
        else:
            tiny = np.ones_like(scale, dtype=bool)

        return constant | tiny

    def _trim_history(self) -> None:
        # Only the last n_scale samples are needed. Trimming every push is slow, so trim
        # once the history has grown to twice that.
        if len(self.filtered_history) > 2 * self.n_scale:
            self.filtered_history = self.filtered_history[-self.n_scale:]
            self.raw_history = self.raw_history[-self.n_scale:]


class SlidingWaveform:
    """Packets -> breathing waveform: the three stages above, chained."""

    def __init__(self, fs: float = FS, band: tuple[float, float] = BAND, window_s: float = WINDOW_S,
                 hop_s: float = HOP_S, scale_s: float = SCALE_S):
        self.resampler = Resampler(fs)
        self.bandpass = CausalBandpass(fs, band)
        self.combiner = Combiner(fs, window_s, hop_s, scale_s)

    def push(self, frame: Frame) -> list[tuple[float, float]]:
        """Take one frame; return the newly determined (time, waveform value) samples."""
        out = []

        for grid_t, raw in self.resampler.push(frame):
            for t, raw_t, filtered in self.bandpass.push(grid_t, raw):
                out += self.combiner.push(t, raw_t, filtered)

        return out
