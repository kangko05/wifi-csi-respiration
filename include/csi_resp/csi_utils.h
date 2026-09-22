#pragma once

#include <stddef.h>
#include <complex.h>

#include "csi_record.h"

#define CSI_PI 3.14159265358979323846264338327950288

float csi_abs(float re, float im);

void csi_amplitude(const csi_frame_t *frame, float *amp);

/* 복소수 순방향 FFT, exp(-j*2*pi*k*n/N), 정규화 없음.
 * n은 1 이상인 2의 거듭제곱. re/im은 각각 n개이며 서로 겹치지 않아야 한다.
 * 입력을 FFT 결과로 덮어쓴다. 실수 입력이면 im을 0으로 채운다.
 * 성공 0, 잘못된 인자이면 1. 내부 메모리 할당 없음. */
int csi_fft(float *re, float *im, size_t n);

/* 역 FFT. csi_fft와 같은 배열 조건이며 결과를 n으로 나눈다. */
int csi_ifft(float *re, float *im, size_t n);

/* Caller-owned interleaved double cos/sin table (n doubles = n*8 bytes).
 * Initialize once for a power-of-two n >= 2. The plan can serve any smaller
 * power-of-two transform. Keep the table alive and immutable while in use. */
typedef struct {
    size_t n;
    const double *twiddles;
} csi_fft_plan_t;
int csi_fft_plan_init(csi_fft_plan_t *plan, size_t n,
                      double *table, size_t table_doubles);
int csi_fft_planned(float *re, float *im, size_t n, const csi_fft_plan_t *plan);
int csi_ifft_planned(float *re, float *im, size_t n, const csi_fft_plan_t *plan);

/* 위상 경로용 double 복소 배열. 위 float API와 같은 FFT 계산부를 사용.
 * n 조건/반환값은 위와 같고, 내부 메모리 할당 없이 입력을 덮어쓴다. */
int csi_fft_complex(double complex *values, size_t n);
int csi_ifft_complex(double complex *values, size_t n);

/* 유한한 값 n개의 중앙값. 입력 배열을 정렬한다. NULL/빈 배열이면 NAN. */
double csi_median(double *values, size_t n);

/* Experimental single-precision butterflies; caller-owned cached plan. */
int csi_fft_float_planned(float *re, float *im, size_t n,
                          const csi_fft_plan_t *plan, int inverse);

/* Experimental Q15 FFT with per-stage block scaling. Caller owns the table.
 * Transform is unnormalized in either direction except for the returned
 * power-of-two scale: DFT(input) ~= output * 2^shift (inverse sign when inverse).
 * Divide by n as well to obtain a normalized inverse. No allocation.
 * Plan tables must be initialized here and remain immutable. */
typedef struct {
    size_t n;
    const int16_t *twiddles;
} csi_fft_q15_plan_t;
int csi_fft_q15_plan_init(csi_fft_q15_plan_t *plan, size_t n,
                          int16_t *table, size_t table_elements);
int csi_fft_q15(int16_t *re, int16_t *im, size_t n,
                 const csi_fft_q15_plan_t *plan, int inverse, unsigned *shift);
/* Quantize, transform, and restore the float API's scale. scratch has 2*n
 * int16 elements; it must not overlap re/im/table. Conversion cost included. */
int csi_fft_q15_float(float *re, float *im, size_t n,
                       const csi_fft_q15_plan_t *plan, int16_t *scratch,
                       size_t scratch_elements, int inverse);

/* Raw int8 I/Q magnitude, unsigned fixed point with 12 fractional bits.
 * Nearest integer sqrt; maximum error is half a Q12 unit. */
uint32_t csi_abs_q12(int8_t re, int8_t im);
