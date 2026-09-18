#pragma once

#include <stddef.h>

typedef struct {
  float fs;           /* 보간된 입력의 샘플링 주파수 [Hz] */
  float target_fs;    /* 목표 출력 주파수 [Hz], 업샘플링은 하지 않음 */
  float half_width_s; /* 필터 중앙 앞뒤로 사용할 시간 [초] */
  float beta;         /* Kaiser 창 파라미터 */
} firwin_config_t;

typedef struct {
  size_t down;      /* 정수 다운샘플링 비율 */
  size_t n_taps;    /* 필요한 계수 개수 */
  double output_fs; /* 실제 출력 주파수: fs / down */
  double fc;        /* 차단 주파수: 0.4 * output_fs */
} firwin_info_t;

/* 설정으로 길이와 주파수를 계산한다. 성공 1, 잘못된 설정이면 0.
 * Python처럼 down=max(1, round(fs/target_fs)), .5는 가까운 짝수로 반올림.
 * n_taps = 2 * ceil(half_width_s * fs) + 1.
 * fs, target_fs, half_width_s는 유한한 양수, beta는 유한한 0 이상 값.
 */
int firwin_info(const firwin_config_t *config, firwin_info_t *info);

/* 위 설정으로 Kaiser 저역통과 FIR 계수를 생성한다.
 * coeffs 용량은 firwin_info()의 n_taps로 준비한다.
 * 반환값: 생성된 계수 개수. 오류/공간 부족이면 0이며 출력은 사용하지 않는다.
 * 설정 시 한 번 생성해 재사용한다. 필터 적용과 다운샘플링은 별도 단계다.
 */
size_t firwin_coeff(const firwin_config_t *config, float *coeffs,
                    size_t max_coeffs);

/* 시간 방향 FIR 합성곱 (패딩 없이 valid 출력).
 * input[g * n_bins + k]: 시간 g, 열 k의 값. 출력도 같은 열 수의 행 우선 배열.
 * coeffs: 홀수 개의 FIR 계수.
 * output은 input/coeffs와 겹치지 않는 별도 공간을 호출자가 준비한다.
 * max_output_rows는 출력 행 용량 (float 개수가 아님).
 * 출력 행 수 = n_rows - n_taps + 1. 입력 부족/인자 오류/공간 부족이면 0.
 * 출력 g의 시각은 입력 g + (n_taps-1)/2의 시각에 해당한다.
 * 샘플링 간격은 유지한다. 열별 eligible 마스크는 호출자가 그대로 유지한다.
 */
size_t fir_filter_valid(const float *input, size_t n_rows, size_t n_bins,
                        const float *coeffs, size_t n_taps, float *output,
                        size_t max_output_rows);

/* Fourth-order Butterworth bandpass, forward/backward SOS with 27 samples
 * of odd edge padding (SciPy butter(4)+sosfiltfilt defaults). In-place.
 * Full-record noncausal filter; no claim of clean edge support. 0 success. */
int butter_bandpass_zero_phase(double *x, size_t n, double fs,
                               double low_hz, double high_hz);
