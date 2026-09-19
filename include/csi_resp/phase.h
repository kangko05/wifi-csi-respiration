#pragma once
#include "csi_record.h"
#include <stddef.h>

/* Legacy phase profile: 117 raw bins, nulls 57..59, assumed 312.5 kHz
 * spacing. This is a compatibility assumption, not a measured RF layout. */
enum { CSI_PHASE_FLAT = 1, CSI_PHASE_WEAK = 2, CSI_PHASE_DISAGREEMENT = 4 };

typedef struct {
  size_t n_samples, n_retained, n_peaks;
  double start_s, sample_rate_hz, duration_s, max_gap_s;
  double bpm, spectral_bpm, peak_count_bpm, sharpness, agreement_bpm;
  int accepted, peak_fallback;
  unsigned reasons;
  int selected_bins[5]; /* original buffer column indices */
} csi_phase_result_t;

/* Full-window RMS -> LoS WLS phase correction -> complex interpolation ->
 * mean removal -> coherent spectrum / projected waveform peak counting.
 * Legacy defaults fixed for comparison: .05..1 Hz, .005 Hz grid,
 * sharpness > 1.55 and agreement < 3 bpm. No amplitude/ACF gate.
 * Returns 0 on success (including withheld); 1 on invalid input/allocation.
 * Intermediate buffers owned/freed internally. Offline full-window memory. */
int csi_phase_process(const csi_frame_t *frames, size_t count,
                      csi_phase_result_t *result);
