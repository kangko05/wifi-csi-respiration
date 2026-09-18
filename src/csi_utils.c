#include <math.h>

#include "csi_resp/csi_record.h"
#include "csi_resp/csi_utils.h"

float csi_abs(float re, float im) { return sqrtf(re * re + im * im); }

void csi_amplitude(const csi_frame_t *frame, float *amp) {
    for (uint16_t k = 0; k < frame->n; k++) {
        if (frame->valid[k]) {
            float re = frame->re[k];
            float im = frame->im[k];

            amp[k] = csi_abs(re, im);
        } else {
            amp[k] = 0.0f;
        }
    }
}

int csi_fft(float *re, float *im, size_t n) {
    if (re == NULL || im == NULL || re == im || n == 0 || (n & (n - 1)) != 0 ||
        n > SIZE_MAX / sizeof(float)) {
        return 1;
    }

    /* radix-2 FFT를 위한 비트 역순 재배열. */
    for (size_t i = 1, j = 0; i < n; i++) {
        size_t bit = n >> 1;
        while (j & bit) {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if (i < j) {
            float tmp = re[i];
            re[i] = re[j];
            re[j] = tmp;
            tmp = im[i];
            im[i] = im[j];
            im[j] = tmp;
        }
    }

    const double pi = 3.14159265358979323846;
    for (size_t length = 2; length <= n;) {
        size_t half = length / 2;
        /* 같은 단계의 모든 블록은 회전 계수를 공유한다. */
        for (size_t j = 0; j < half; j++) {
            double angle = -2.0 * pi * (double)j / (double)length;
            double wr = cos(angle);
            double wi = sin(angle);
            for (size_t base = 0; base < n; base += length) {
                size_t a = base + j;
                size_t b = a + half;
                double tr = wr * re[b] - wi * im[b];
                double ti = wr * im[b] + wi * re[b];
                double ar = re[a], ai = im[a];
                re[a] = (float)(ar + tr);
                im[a] = (float)(ai + ti);
                re[b] = (float)(ar - tr);
                im[b] = (float)(ai - ti);
            }
        }
        if (length == n)
            break; /* 다음 배수 계산의 overflow 방지 */
        length *= 2;
    }
    return 0;
}

int csi_ifft(float *re, float *im, size_t n) {
    if (re == NULL || im == NULL || re == im || n == 0 || (n & (n - 1)) != 0 ||
        n > SIZE_MAX / sizeof(float))
        return 1;

    for (size_t i = 0; i < n; i++)
        im[i] = -im[i];

    if (csi_fft(re, im, n) != 0)
        return 1;

    for (size_t i = 0; i < n; i++) {
        re[i] = (float)((double)re[i] / n);
        im[i] = (float)(-(double)im[i] / n);
    }

    return 0;
}
