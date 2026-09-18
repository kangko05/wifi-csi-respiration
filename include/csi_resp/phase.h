#pragma once
#include "csi_record.h"
#include <stddef.h>

/* Legacy phase profile: 117 raw bins, nulls 57..59, assumed 312.5 kHz
 * spacing. This is a compatibility assumption, not a measured RF layout. */
enum { CSI_PHASE_FLAT = 1, CSI_PHASE_WEAK = 2, CSI_PHASE_DISAGREEMENT = 4 };

typedef enum {
    CSI_PHASE_BIN_SKIP, /* 분석/배치 검사에서 제외 */
    CSI_PHASE_BIN_USED, /* 사용할 열: 실제 주파수 오프셋을 지정 */
    CSI_PHASE_BIN_NULL  /* null 크기 검사에만 사용 */
} csi_phase_bin_role_t;

typedef struct {
    double frequency_hz;
    csi_phase_bin_role_t role;
} csi_phase_bin_t;

typedef struct {
    size_t n_bins; /* 배치 설명 길이. 입력 frames[0].n과 같아야 한다. */
    csi_phase_bin_t bins[CSI_MAX_SUBCARRIERS];
    double subcarrier_spacing_hz; /* 초기 시간 오차 추정에 사용 */
    double min_hz, max_hz, frequency_step_hz;
    size_t top_columns, whiten_bins;
    double max_gap_s, min_duration_s, max_null_ratio;
    double min_sharpness, max_agreement_bpm;
} csi_phase_config_t;

/* 기존 117열 HT40 가정의 기본 배치/설정. 다른 신호는 호출부에서 지정한다. */
csi_phase_config_t csi_phase_default_config(void);

typedef struct {
    size_t n_samples, n_retained, n_peaks;
    double start_s, sample_rate_hz, duration_s, max_gap_s;
    double bpm, spectral_bpm, peak_count_bpm, sharpness, agreement_bpm;
    int accepted, peak_fallback;
    unsigned reasons;
    /* 배열 용량은 프로토콜 상한, 실제 선택 개수는 n_selected. */
    int selected_bins[CSI_MAX_SUBCARRIERS];
    size_t n_selected, n_columns, n_frequencies;
} csi_phase_result_t;

/* Full-window RMS -> LoS WLS phase correction -> complex interpolation ->
 * mean removal -> coherent spectrum / projected waveform peak counting.
 * Legacy defaults fixed for comparison: .05..1 Hz, .005 Hz grid,
 * sharpness > 1.55 and agreement < 3 bpm. No amplitude/ACF gate.
 * Returns 0 on success (including withheld); 1 on invalid input/allocation.
 * Intermediate buffers owned/freed internally. Offline full-window memory. */
int csi_phase_process(const csi_frame_t *frames, size_t count,
                      csi_phase_result_t *result);

/* 설정은 읽기 전용이며 호출 동안 유효해야 한다. 기본 함수와 같은 반환 규칙.
 * USED 열은 frequency_hz 오름차순이어야 한다. 물리 배치는 자동 추측하지 않는다.
 * 열 수는 USED 목록에서, 주파수 격자는 min/max/step에서 계산한다.
 * max_hz 이하의 격자만 사용하며 top_columns는 유효 열 수 이하여야 한다. */
int csi_phase_process_with_config(const csi_frame_t *frames, size_t count,
                                  const csi_phase_config_t *config,
                                  csi_phase_result_t *result);
