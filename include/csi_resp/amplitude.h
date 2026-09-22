#pragma once

#include <stddef.h>
#include <stdint.h>

#include "csi_record.h"
#include "filters.h"
#include "csi_utils.h"

/** 보간 격자 간격 [us] (100Hz) */
#define CSI_INTERP_STEP_US 10000
/** 칸별 유효 샘플 최대 간격 [us], 창 시작/끝까지 포함 */
#define CSI_INTERP_MAX_GAP_US 100000
/** 칸별 유효 샘플 비율 하한 */
#define CSI_INTERP_MIN_FRACTION 0.9f
/** 칸별 유효 샘플 수 하한 */
#define CSI_INTERP_MIN_PACKETS 2

typedef struct {
    double min_hz, max_hz;
    double min_duration_s, min_cycles;
    double min_concentration, min_acf;
    double agreement_resolution_bins;
    double flat_relative_floor;
    size_t fft_oversampling;
} csi_amp_analysis_config_t;

/* Python PeriodicityConfig의 기본값. */
csi_amp_analysis_config_t csi_amp_default_analysis_config(void);

enum {
    CSI_AMP_EXCLUDED       = 1u << 0,
    CSI_AMP_FLAT           = 1u << 1,
    CSI_AMP_SHORT          = 1u << 2,
    CSI_AMP_BAND_EDGE      = 1u << 3,
    CSI_AMP_FEW_CYCLES     = 1u << 4,
    CSI_AMP_DIFFUSE        = 1u << 5,
    CSI_AMP_WEAK_ACF       = 1u << 6,
    CSI_AMP_DISAGREEMENT   = 1u << 7,
    CSI_AMP_NOT_SELECTED   = 1u << 8 /* experimental preselection */
};

typedef struct {
    int eligible;
    int has_psd, has_acf;
    int accepted;              /* 주기 후보 조건 통과. 호흡 검출 확정이 아님. */
    uint32_t reasons;          /* 위 비트 플래그 조합 */
    double psd_hz, psd_bpm, psd_peak;
    double acf_hz, acf_bpm, acf_peak, acf_lag_s;
    double concentration, score, cycles;
} csi_amp_bin_result_t;

/* 배열 소유권 이전 없이 호출자가 준비한 결과에 요약을 복사한다.
 * FFT/PSD/ACF 전체 배열은 중간 버퍼이며 반환하지 않는다.
 * has_psd/has_acf가 0이면 해당 수치 필드는 사용하지 않는다. */
typedef struct {
    size_t n_bins, n_samples, n_fft;
    uint32_t start_us;         /* 첫 프레임 기준 분석 시작 시각 */
    uint64_t step_us;
    double sample_rate_hz, duration_s, resolution_hz;
    int best_bin;             /* 최고 점수 진단 후보, 없으면 -1 */
    int selected_bin;         /* 조건을 통과한 최고 점수 후보, 없으면 -1 */
    csi_amp_bin_result_t bins[CSI_MAX_SUBCARRIERS];
} csi_amp_result_t;

/* {0}으로 초기화하고 init -> process(여러 윈도우) -> free 순서로 사용.
 * coeffs는 컨텍스트 소유이며 직접 수정/해제하거나 구조체를 복사하지 않는다. */
typedef struct {
    firwin_info_t filter_info;
    float *coeffs;
    csi_amp_analysis_config_t analysis; /* init에서 기본값 설정, 호출 전 조절 가능 */
} csi_amp_context_t;

/* 성공 0, 실패 1. config.fs는 내부에서 보간 주파수로 맞춘다.
 * 설정 변경 시 먼저 free 후 다시 init한다. */
int csi_amp_init(csi_amp_context_t *context, const firwin_config_t *config);
void csi_amp_free(csi_amp_context_t *context);

/* 보간 -> FIR/다운샘플링 -> 추세 제거 -> PSD/ACF -> 후보 판정.
 * 성공 0, 입력/설정/메모리 오류 1 (실패 시 result 내용 사용 금지).
 * 성공해도 selected_bin==-1일 수 있다. 이는 지지되는 주기 후보가 없다는 뜻.
 * 중간 배열은 내부에서 해제한다. result는 호출자 소유 (큰 구조체이므로
 * 보드에서는 static 또는 heap 권장). */
int csi_amp_process(const csi_frame_t *frames, size_t n_frames,
                    const csi_amp_context_t *context, csi_amp_result_t *result);

/* Immutable chronological raw records (including a ring via a reader).
 * The reader must return stable, non-NULL records throughout the call.
 * Optional yield runs between columns; it must not change the source.
 * Input must already satisfy csi_preprocess validation, with equal lengths.
 * Gain is deliberately NOT applied, matching csi_preprocess + csi_amp_process.
 */
typedef enum {
    CSI_AMP_STAGE_VALIDATE, CSI_AMP_STAGE_ELIGIBILITY, CSI_AMP_STAGE_SETUP,
    CSI_AMP_STAGE_INTERPOLATE, CSI_AMP_STAGE_FIR, CSI_AMP_STAGE_DETREND,
    CSI_AMP_STAGE_PSD, CSI_AMP_STAGE_ACF, CSI_AMP_STAGE_SCORE,
    CSI_AMP_STAGE_YIELD, CSI_AMP_STAGE_CLEANUP, CSI_AMP_STAGE_COUNT
} csi_amp_stage_t;

typedef struct {
    const csi_record_t *(*read)(const void *user, size_t index);
    const void *user;
    void (*yield)(void *user);
    void *yield_user;
    /* Optional stage-entry observer; must not mutate input/context/result.
     * bin is zero-based; bins==0 means layout is not known yet. */
    void (*trace)(void *user, csi_amp_stage_t stage, size_t bin, size_t bins);
    void *trace_user;
    const csi_fft_plan_t *fft_plan; /* optional precomputed twiddles */
    /* Experimental, zero preserves baseline. Float affects FFT and FIR only.
     * max_bins preselects by sampled normalized amplitude variance. */
    int float_math;
    size_t max_bins;
    /* Optional precomputed, time-major FIR/downsampled amplitudes, matching
     * this input's exact common grid and context. Caller owns storage. */
    const float *prepared;
    size_t prepared_rows;
    /* B0: replaces FFTs only; NULL keeps the selected floating-point path. */
    const csi_fft_q15_plan_t *q15_plan;
    /* Experimental Q12 amplitude/interpolation + Q23 FIR, independent of FFT.
     * Not combined with externally prepared data. */
    int fixed_preprocess;
    /* Optional read-only observer before detrending. Data is borrowed. */
    void (*observe_reduced)(void *user, size_t bin, const float *data, size_t n);
    void *observe_user;
} csi_amp_raw_source_t;

/* Same result semantics as csi_amp_process, with O(time) temporary storage.
 * Reuses one column and computes only retained FIR output positions. */
int csi_amp_process_raw(const csi_amp_raw_source_t *source, size_t n_records,
                        const csi_amp_context_t *context,
                        csi_amp_result_t *result);

/* Experimental per-window streaming preprocessor. Complete, fixed-width raw
 * records only (no first_word_invalid); gaps <= 100 ms. Reset by free/init.
 * The context is borrowed and must outlive the stream. Finish once, then use
 * data/rows as raw_source.prepared. This is not a sliding-window engine. */
typedef struct csi_amp_stream csi_amp_stream_t;
csi_amp_stream_t *csi_amp_stream_create(const csi_amp_context_t *context,
                                        size_t bins, size_t capacity, int float_math);
int csi_amp_stream_push(csi_amp_stream_t *stream, const csi_record_t *record);
int csi_amp_stream_finish(csi_amp_stream_t *stream);
const float *csi_amp_stream_data(const csi_amp_stream_t *stream);
size_t csi_amp_stream_rows(const csi_amp_stream_t *stream);
size_t csi_amp_stream_bytes(const csi_amp_stream_t *stream);
void csi_amp_stream_free(csi_amp_stream_t *stream);
