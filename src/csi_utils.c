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

static void fft_butterflies(fft_buffer buffer, size_t n,
                             const csi_fft_plan_t *plan) {
    for (size_t length = 2; length <= n;) {
        size_t half = length / 2;
        /* 같은 단계의 모든 블록은 회전 계수를 공유한다. */
        for (size_t j = 0; j < half; j++) {
            double wr, wi;
            if (plan) {
                size_t index = 2 * j * (plan->n / length);
                wr = plan->twiddles[index];
                wi = plan->twiddles[index + 1];
            } else {
                double angle = -2.0 * CSI_PI * (double)j / (double)length;
                wr = cos(angle);
                wi = sin(angle);
            }

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

static void fft_transform(fft_buffer buffer, size_t n, int inverse,
                           const csi_fft_plan_t *plan) {
    /* 역변환은 conjugate -> FFT -> conjugate / n. */
    if (inverse) {
        for (size_t i = 0; i < n; i++) {
            double re, im;
            fft_read(buffer, i, &re, &im);
            fft_write(buffer, i, re, -im);
        }
    }

    fft_bit_reverse(buffer, n);
    fft_butterflies(buffer, n, plan);

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

    fft_transform((fft_buffer){.re = re, .im = im}, n, 0, NULL);

    return 0;
}

int csi_ifft(float *re, float *im, size_t n) {
    if (re == NULL || im == NULL || re == im ||
        !fft_length_valid(n, sizeof(*re))) {
        return 1;
    }

    fft_transform((fft_buffer){.re = re, .im = im}, n, 1, NULL);

    return 0;
}

int csi_fft_complex(double complex *values, size_t n) {
    if (values == NULL || !fft_length_valid(n, sizeof(*values))) {
        return 1;
    }

    fft_transform((fft_buffer){.values = values}, n, 0, NULL);

    return 0;
}

int csi_ifft_complex(double complex *values, size_t n) {
    if (values == NULL || !fft_length_valid(n, sizeof(*values))) {
        return 1;
    }

    fft_transform((fft_buffer){.values = values}, n, 1, NULL);

    return 0;
}

int csi_fft_plan_init(csi_fft_plan_t *plan, size_t n,
                      double *table, size_t table_doubles) {
    if (!plan || !table || n < 2 || !fft_length_valid(n, sizeof(*table)) ||
        table_doubles < n) {
        return 1;
    }
    for (size_t j = 0; j < n / 2; ++j) {
        double angle = -2.0 * CSI_PI * (double)j / (double)n;
        table[2 * j] = cos(angle);
        table[2 * j + 1] = sin(angle);
    }
    *plan = (csi_fft_plan_t){.n = n, .twiddles = table};
    return 0;
}

static int fft_planned(float *re, float *im, size_t n,
                        const csi_fft_plan_t *plan, int inverse) {
    if (!re || !im || re == im || !fft_length_valid(n, sizeof(*re)) ||
        !plan || !plan->twiddles || plan->n < 2 || n > plan->n ||
        !fft_length_valid(plan->n, sizeof(double))) {
        return 1;
    }
    fft_transform((fft_buffer){.re = re, .im = im}, n, inverse, plan);
    return 0;
}

int csi_fft_planned(float *re, float *im, size_t n, const csi_fft_plan_t *plan) {
    return fft_planned(re, im, n, plan, 0);
}

int csi_ifft_planned(float *re, float *im, size_t n, const csi_fft_plan_t *plan) {
    return fft_planned(re, im, n, plan, 1);
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

int csi_fft_float_planned(float *re, float *im, size_t n,
                          const csi_fft_plan_t *plan, int inverse) {
    if (!re || !im || re == im || !fft_length_valid(n, sizeof(*re)) ||
        !plan || !plan->twiddles || n > plan->n ||
        !fft_length_valid(plan->n, sizeof(double))) return 1;
    if (inverse) for (size_t i = 0; i < n; ++i) im[i] = -im[i];
    for (size_t i = 1, j = 0; i < n; ++i) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            float tmp = re[i]; re[i] = re[j]; re[j] = tmp;
            tmp = im[i]; im[i] = im[j]; im[j] = tmp;
        }
    }
    for (size_t length = 2; length <= n; length <<= 1) {
        size_t half = length / 2;
        for (size_t j = 0; j < half; ++j) {
            size_t t = 2 * j * (plan->n / length);
            float wr = (float)plan->twiddles[t];
            float wi = (float)plan->twiddles[t + 1];
            for (size_t base = 0; base < n; base += length) {
                size_t a = base + j, b = a + half;
                float tr = wr * re[b] - wi * im[b];
                float ti = wr * im[b] + wi * re[b];
                float ar = re[a], ai = im[a];
                re[a] = ar + tr; im[a] = ai + ti;
                re[b] = ar - tr; im[b] = ai - ti;
            }
        }
        if (length == n) break;
    }
    if (inverse) for (size_t i = 0; i < n; ++i) {
        re[i] /= (float)n; im[i] /= -(float)n;
    }
    return 0;
}

int csi_fft_q15_plan_init(csi_fft_q15_plan_t *plan, size_t n,
                          int16_t *table, size_t table_elements) {
    if (!plan || !table || n < 2 || !fft_length_valid(n, sizeof(*table)) ||
        table_elements < n) return 1;
    for (size_t j = 0; j < n / 2; ++j) {
        double angle = -2.0 * CSI_PI * (double)j / (double)n;
        /* Symmetric bounds keep inverse twiddle negation representable. */
        table[2*j] = (int16_t)lround(32767.0 * cos(angle));
        table[2*j+1] = (int16_t)lround(32767.0 * sin(angle));
    }
    *plan = (csi_fft_q15_plan_t){.n=n, .twiddles=table};
    return 0;
}

static int32_t q15_round(int32_t value) {
    /* Products from a unit twiddle cannot approach INT32_MIN/MAX. */
    return value >= 0 ? (value + 16384) / 32768 :
                       -((-value + 16384) / 32768);
}

int csi_fft_q15(int16_t *re, int16_t *im, size_t n,
                 const csi_fft_q15_plan_t *plan, int inverse, unsigned *shift) {
    if (!re || !im || re == im || !shift ||
        !fft_length_valid(n, sizeof(*re)) || !plan || !plan->twiddles ||
        n > plan->n || !fft_length_valid(plan->n, sizeof(*plan->twiddles))) return 1;
    *shift = 0;
    for (size_t i=1, j=0; i<n; ++i) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            int16_t tmp = re[i]; re[i] = re[j]; re[j] = tmp;
            tmp = im[i]; im[i] = im[j]; im[j] = tmp;
        }
    }
    for (size_t length=2; length<=n; length<<=1) {
        int32_t maximum = 0;
        for (size_t i=0; i<n; ++i) {
            int32_t magnitude = abs((int)re[i]) + abs((int)im[i]);
            if (magnitude > maximum) maximum = magnitude;
        }
        /* Each butterfly component is bounded by the sum of its inputs' L1
         * norms. Scale only when needed; fixed 1/N scaling loses sparse data. */
        unsigned bits = 0;
        while (maximum > 16383) { maximum = (maximum + 1) / 2; ++bits; }
        if (bits) {
            int divisor = 1 << bits;
            for (size_t i=0; i<n; ++i) { re[i] /= divisor; im[i] /= divisor; }
            *shift += bits;
        }
        size_t half = length / 2;
        for (size_t j=0; j<half; ++j) {
            size_t t = 2*j*(plan->n/length);
            int32_t wr = plan->twiddles[t], wi = plan->twiddles[t+1];
            if (inverse) wi = -wi;
            for (size_t base=0; base<n; base+=length) {
                size_t a=base+j, b=a+half;
                int32_t br=re[b], bi=im[b], ar=re[a], ai=im[a];
                int32_t tr=q15_round(wr*br-wi*bi);
                int32_t ti=q15_round(wr*bi+wi*br);
                re[a]=(int16_t)(ar+tr); im[a]=(int16_t)(ai+ti);
                re[b]=(int16_t)(ar-tr); im[b]=(int16_t)(ai-ti);
            }
        }
        if (length == n) break;
    }
    return 0;
}

int csi_fft_q15_float(float *re, float *im, size_t n,
                       const csi_fft_q15_plan_t *plan, int16_t *scratch,
                       size_t scratch_elements, int inverse) {
    if (!re || !im || re == im || !scratch || !plan || !plan->twiddles ||
        !fft_length_valid(n, sizeof(*re)) || n > plan->n ||
        !fft_length_valid(plan->n, sizeof(*plan->twiddles)) ||
        n > SIZE_MAX/2 || scratch_elements < 2*n) return 1;
    float peak = 0;
    for (size_t i=0; i<n; ++i) {
        if (!isfinite(re[i]) || !isfinite(im[i])) return 1;
        peak = fmaxf(peak, fmaxf(fabsf(re[i]), fabsf(im[i])));
    }
    if (peak == 0) return 0;
    /* Both complex components together fit the initial L1 bound. */
    float gain = 8191.0f / peak;
    int16_t *qr=scratch, *qi=scratch+n;
    if (isfinite(gain) && gain > 0) {
        for (size_t i=0; i<n; ++i) {
            qr[i]=(int16_t)lroundf(re[i]*gain);
            qi[i]=(int16_t)lroundf(im[i]*gain);
        }
    } else {
        for (size_t i=0; i<n; ++i) {
            qr[i]=(int16_t)lround((double)re[i]/peak*8191.0);
            qi[i]=(int16_t)lround((double)im[i]/peak*8191.0);
        }
    }
    unsigned shift;
    if (csi_fft_q15(qr, qi, n, plan, inverse, &shift)) return 1;
    double scale = ldexp((double)peak/8191.0, (int)shift);
    if (inverse) scale /= n;
    float scale_f = (float)scale;
    for (size_t i=0; i<n; ++i) {
        if (isfinite(scale_f) && scale_f > 0) {
            re[i]=qr[i]*scale_f; im[i]=qi[i]*scale_f;
        } else {
            re[i]=(float)(qr[i]*scale); im[i]=(float)(qi[i]*scale);
        }
        if (!isfinite(re[i]) || !isfinite(im[i])) return 1;
    }
    return 0;
}

static uint32_t magnitude_q12_from_power(uint32_t power) {
    uint64_t value = (uint64_t)power << 24;
    uint64_t remainder = value, root = 0, bit = UINT64_C(1) << 62;
    while (bit > remainder) bit >>= 2;
    while (bit) {
        if (remainder >= root + bit) {
            remainder -= root + bit;
            root = (root >> 1) + bit;
        } else root >>= 1;
        bit >>= 2;
    }
    /* Round sqrt(value) to nearest: remainder >= root+1 rounds upward. */
    if (value - root*root > root) ++root;
    return (uint32_t)root;
}

uint32_t csi_abs_q12(int8_t re, int8_t im) {
    return magnitude_q12_from_power((uint32_t)((int)re*re + (int)im*im));
}
