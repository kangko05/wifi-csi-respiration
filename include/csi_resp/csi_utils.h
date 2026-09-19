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

/* 위상 경로용 double 복소 배열. 위 float API와 같은 FFT 계산부를 사용.
 * n 조건/반환값은 위와 같고, 내부 메모리 할당 없이 입력을 덮어쓴다. */
int csi_fft_complex(double complex *values, size_t n);
int csi_ifft_complex(double complex *values, size_t n);

/* 유한한 값 n개의 중앙값. 입력 배열을 정렬한다. NULL/빈 배열이면 NAN. */
double csi_median(double *values, size_t n);
