#include <math.h>
#include <stdlib.h>

#include "csi_resp/csi_record.h"
#include "csi_resp/csi_utils.h"

float csi_abs(float re, float im) {
    return sqrtf(re * re + im * im);
}

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

/* 저장 형식만 구분한다. 재배열과 butterfly 계산은 두 API가 공유한다. */
typedef struct {
    float *re, *im;
    double complex *values;
} fft_buffer;

static void fft_read(fft_buffer buffer, size_t i, double *re, double *im) {
    if (buffer.values != NULL) {
        *re = creal(buffer.values[i]);
        *im = cimag(buffer.values[i]);
    } else {
        *re = buffer.re[i];
        *im = buffer.im[i];
    }
}

static void fft_write(fft_buffer buffer, size_t i, double re, double im) {
    if (buffer.values != NULL) {
        buffer.values[i] = re + I * im;
    } else {
        buffer.re[i] = (float)re;
        buffer.im[i] = (float)im;
    }
}

static int fft_length_valid(size_t n, size_t element_size) {
    return n > 0 && (n & (n - 1)) == 0 && n <= SIZE_MAX / element_size;
}

static void fft_bit_reverse(fft_buffer buffer, size_t n) {
    /* radix-2 FFT를 위한 비트 역순 재배열. */
    for (size_t i = 1, j = 0; i < n; i++) {
        size_t bit = n >> 1;

        while (j & bit) {
            j ^= bit;
            bit >>= 1;
        }

        j ^= bit;

        if (i < j) {
            double ar, ai, br, bi;
            fft_read(buffer, i, &ar, &ai);
            fft_read(buffer, j, &br, &bi);

            fft_write(buffer, i, br, bi);
            fft_write(buffer, j, ar, ai);
        }
    }
}

static void fft_butterflies(fft_buffer buffer, size_t n) {
    for (size_t length = 2; length <= n;) {
        size_t half = length / 2;
        /* 같은 단계의 모든 블록은 회전 계수를 공유한다. */
        for (size_t j = 0; j < half; j++) {
            double angle = -2.0 * CSI_PI * (double)j / (double)length;
            double wr = cos(angle);
            double wi = sin(angle);

            for (size_t base = 0; base < n; base += length) {
                size_t a = base + j;
                size_t b = a + half;
                double ar, ai, br, bi;
                fft_read(buffer, a, &ar, &ai);
                fft_read(buffer, b, &br, &bi);

                double tr = wr * br - wi * bi;
                double ti = wr * bi + wi * br;

                fft_write(buffer, a, ar + tr, ai + ti);
                fft_write(buffer, b, ar - tr, ai - ti);
            }
        }

        if (length == n) {
            break; /* 다음 배수 계산의 overflow 방지 */
        }

        length *= 2;
    }
}

static void fft_transform(fft_buffer buffer, size_t n, int inverse) {
    /* 역변환은 conjugate -> FFT -> conjugate / n. */
    if (inverse) {
        for (size_t i = 0; i < n; i++) {
            double re, im;
            fft_read(buffer, i, &re, &im);
            fft_write(buffer, i, re, -im);
        }
    }

    fft_bit_reverse(buffer, n);
    fft_butterflies(buffer, n);

    if (inverse) {
        for (size_t i = 0; i < n; i++) {
            double re, im;
            fft_read(buffer, i, &re, &im);
            fft_write(buffer, i, re / n, -im / n);
        }
    }
}

int csi_fft(float *re, float *im, size_t n) {
    if (re == NULL || im == NULL || re == im ||
        !fft_length_valid(n, sizeof(*re))) {
        return 1;
    }

    fft_transform((fft_buffer){.re = re, .im = im}, n, 0);

    return 0;
}

int csi_ifft(float *re, float *im, size_t n) {
    if (re == NULL || im == NULL || re == im ||
        !fft_length_valid(n, sizeof(*re))) {
        return 1;
    }

    fft_transform((fft_buffer){.re = re, .im = im}, n, 1);

    return 0;
}

int csi_fft_complex(double complex *values, size_t n) {
    if (values == NULL || !fft_length_valid(n, sizeof(*values))) {
        return 1;
    }

    fft_transform((fft_buffer){.values = values}, n, 0);

    return 0;
}

int csi_ifft_complex(double complex *values, size_t n) {
    if (values == NULL || !fft_length_valid(n, sizeof(*values))) {
        return 1;
    }

    fft_transform((fft_buffer){.values = values}, n, 1);

    return 0;
}

static int compare_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;

    return (x > y) - (x < y);
}

double csi_median(double *values, size_t n) {
    if (values == NULL || n == 0) {
        return NAN;
    }

    qsort(values, n, sizeof(*values), compare_double);

    return n % 2 ? values[n / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
}
