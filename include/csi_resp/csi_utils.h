#pragma once

#include <stddef.h>

#include "csi_record.h"

float csi_abs(float re, float im);

void csi_amplitude(const csi_frame_t *frame, float *amp);

/* 복소수 순방향 FFT, exp(-j*2*pi*k*n/N), 정규화 없음.
 * n은 1 이상인 2의 거듭제곱. re/im은 각각 n개이며 서로 겹치지 않아야 한다.
 * 입력을 FFT 결과로 덮어쓴다. 실수 입력이면 im을 0으로 채운다.
 * 성공 0, 잘못된 인자이면 1. 내부 메모리 할당 없음. */
int csi_fft(float *re, float *im, size_t n);

/* 역 FFT. csi_fft와 같은 배열 조건이며 결과를 n으로 나눈다. */
int csi_ifft(float *re, float *im, size_t n);
