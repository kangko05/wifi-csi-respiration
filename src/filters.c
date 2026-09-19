#include "csi_resp/filters.h"
#include "csi_resp/csi_utils.h"

#include <complex.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static int design_butter_bandpass(double fs, double low_hz, double high_hz,
                                  double sos[4][5], double zi[4][2]) {
    double lo = 4 * tan(CSI_PI * low_hz / fs);
    double hi = 4 * tan(CSI_PI * high_hz / fs);
    double bw = hi - lo, center2 = lo * hi;
    double complex poles[4], denominator = 1;
    int count = 0;

    for (int k = 0; k < 4; ++k) {
        double complex p = -cexp(I * CSI_PI * (2 * k - 3) / 8) * bw / 2;
        double complex root = csqrt(p * p - center2);

        for (int sign = -1; sign <= 1; sign += 2) {
            double complex analog = p + sign * root;
            denominator *= 4 - analog;

            double complex digital = (4 + analog) / (4 - analog);

            if (cimag(digital) > 0) {
                poles[count++] = digital;
            }
        }
    }

    if (count != 4) {
        return 1;
    }

    /* Least resonant section first, as in scipy's nearest SOS pairing. */
    for (int i = 0; i < 4; ++i) {
        for (int j = i + 1; j < 4; ++j) {
            if (cabs(poles[j]) < cabs(poles[i])) {
                double complex tmp = poles[i];
                poles[i] = poles[j];
                poles[j] = tmp;
            }
        }
    }

    double scale = 1;

    for (int j = 0; j < 4; ++j) {
        /* Four analog zeros at zero contribute (2*fs_normalized)^4. */
        double gain = j == 0 ? pow(4 * bw, 4) / creal(denominator) : 1;
        sos[j][0] = gain;
        sos[j][1] = (j < 2 ? 2 : -2) * gain;
        sos[j][2] = gain;
        sos[j][3] = -2 * creal(poles[j]);
        sos[j][4] = creal(poles[j] * conj(poles[j]));

        double dc =
            (sos[j][0] + sos[j][1] + sos[j][2]) / (1 + sos[j][3] + sos[j][4]);
        zi[j][0] = scale * (dc - sos[j][0]);
        zi[j][1] = scale * (sos[j][2] - sos[j][4] * dc);
        scale *= dc;
    }

    return 0;
}

static void pad_filter_edges(const double *x, size_t n, double *extended) {
    for (size_t i = 0; i < 27; ++i) {
        extended[i] = 2 * x[0] - x[27 - i];
    }

    for (size_t i = 0; i < n; ++i) {
        extended[i + 27] = x[i];
    }

    for (size_t i = 0; i < 27; ++i) {
        extended[n + 27 + i] = 2 * x[n - 1] - x[n - 2 - i];
    }
}

static void filter_sos_forward_backward(double *extended, size_t length,
                                        double sos[4][5], double zi[4][2]) {
    for (int pass = 0; pass < 2; ++pass) {
        double initial = extended[pass ? length - 1 : 0];

        for (int j = 0; j < 4; ++j) {
            double z0 = zi[j][0] * initial, z1 = zi[j][1] * initial;

            for (size_t i = 0; i < length; ++i) {
                size_t index = pass ? length - 1 - i : i;
                double value = extended[index], y = sos[j][0] * value + z0;
                z0 = sos[j][1] * value - sos[j][3] * y + z1;
                z1 = sos[j][2] * value - sos[j][4] * y;
                extended[index] = y;
            }
        }
    }
}

int butter_bandpass_zero_phase(double *x, size_t n, double fs, double low_hz,
                               double high_hz) {
    if (!x || n <= 27 || n > SIZE_MAX / sizeof(double) - 54 || !isfinite(fs) ||
        !isfinite(low_hz) || !isfinite(high_hz) || fs <= 0) {
        return 1;
    }

    high_hz = fmin(high_hz, .99 * fs / 2);

    if (!(0 < low_hz && low_hz < high_hz)) {
        return 1;
    }

    double sos[4][5], zi[4][2];

    if (design_butter_bandpass(fs, low_hz, high_hz, sos, zi)) {
        return 1;
    }

    size_t length = n + 54;
    double *extended = malloc(length * sizeof(*extended));

    if (!extended) {
        return 1;
    }

    pad_filter_edges(x, n, extended);
    filter_sos_forward_backward(extended, length, sos, zi);

    int failed = 0;

    for (size_t i = 0; i < n; ++i) {
        x[i] = extended[i + 27];

        if (!isfinite(x[i])) {
            failed = 1;
        }
    }

    free(extended);

    return failed;
}

int hampel_filter(double *x, size_t n) {
    if (x == NULL || n == 0 || n > SIZE_MAX / sizeof(*x)) {
        return 1;
    }

    double *out = malloc(n * sizeof(*out));

    if (!out) {
        return 1;
    }

    for (size_t i = 0; i < n; ++i) {
        double values[11], dev[11];

        for (int j = -5; j <= 5; ++j) {
            size_t index = j < 0 && i < (size_t)(-j) ? 0 : i + j;

            if (index >= n) {
                index = n - 1;
            }

            values[j + 5] = x[index];
        }

        double med = csi_median(values, 11);

        for (int j = 0; j < 11; ++j) {
            dev[j] = fabs(values[j] - med);
        }

        out[i] =
            fabs(x[i] - med) > 3 * 1.4826 * csi_median(dev, 11) ? med : x[i];
    }

    memcpy(x, out, n * sizeof(*x));
    free(out);

    return 0;
}

/* Kaiser 창에 쓰는 함수: I0(x) = sum((x*x/4)^k / (k!)^2). */
static double bessel_i0(double x) {
    double sum = 1.0;
    double term = 1.0;
    double q = x * x / 4.0;

    for (unsigned k = 1; k < 256; k++) {
        term *= q / ((double)k * k);
        sum += term;
        if (!isfinite(sum)) {
            return NAN;
        }
        if (term <= sum * DBL_EPSILON) {
            return sum;
        }
    }

    return NAN;
}

int firwin_info(const firwin_config_t *config, firwin_info_t *info) {

    if (config == NULL || info == NULL || !isfinite(config->fs) ||
        config->fs <= 0.0f || !isfinite(config->target_fs) ||
        config->target_fs <= 0.0f || !isfinite(config->half_width_s) ||
        config->half_width_s <= 0.0f || !isfinite(config->beta) ||
        config->beta < 0.0f) {
        return 0;
    }

    /* Python round와 동일하게 정확히 .5이면 짝수 쪽을 선택한다. */
    double ratio = (double)config->fs / config->target_fs;
    double down = floor(ratio);
    double fraction = ratio - down;

    if (fraction > 0.5 || (fraction == 0.5 && fmod(down, 2.0) != 0.0)) {
        down += 1.0;
    }

    down = fmax(1.0, down);

    if (down >= (double)SIZE_MAX) {
        return 0;
    }

    /* 앞뒤 각각 half_width_s초 + 중앙 1개. 100Hz, 1초이면 201개. */
    double half_samples = ceil((double)config->half_width_s * config->fs);

    /* 계수 배열의 바이트 수까지 size_t 범위에 들어와야 한다. */
    if (half_samples >= (double)((SIZE_MAX / sizeof(float) - 1) / 2)) {
        return 0;
    }

    info->down = (size_t)down;
    info->n_taps = 2 * (size_t)half_samples + 1;
    info->output_fs = (double)config->fs / down;

    /* 기존 Python의 전환 여유: 출력 나이퀴스트의 80%. */
    info->fc = 0.4 * info->output_fs;

    return 1;
}

size_t firwin_coeff(const firwin_config_t *config, float *coeffs,
                    size_t max_coeffs) {

    firwin_info_t info;

    if (coeffs == NULL || !firwin_info(config, &info) ||
        max_coeffs < info.n_taps) {
        return 0;
    }
    size_t n_taps = info.n_taps;
    size_t half = (n_taps - 1) / 2;
    double beta = config->beta;
    double denominator = bessel_i0(beta);

    if (!isfinite(denominator)) {
        return 0;
    }

    double cutoff = 2.0 * info.fc / config->fs;
    double sum = 0.0;

    for (size_t n = 0; n < n_taps; n++) {
        /* 중앙을 0으로 옮긴 위치: -half, ..., 0, ..., +half. */
        double m = (double)n - (double)half;

        /* cutoff * sinc(cutoff*m), sinc(x) = sin(pi*x)/(pi*x). */
        double h =
            (n == half) ? cutoff : sin(CSI_PI * cutoff * m) / (CSI_PI * m);

        /* 양끝에서 작아지는 대칭 Kaiser 창을 곱한다. */
        double r = m / (double)half;
        double window =
            bessel_i0(beta * sqrt(fmax(0.0, 1.0 - r * r))) / denominator;
        coeffs[n] = (float)(h * window);
        sum += coeffs[n];
    }

    /* 계수 합을 1로 맞춰 일정한 입력의 크기를 유지한다. */
    if (!isfinite(sum) || fabs(sum) < DBL_EPSILON) {
        return 0;
    }

    for (size_t n = 0; n < n_taps; n++) {
        coeffs[n] = (float)(coeffs[n] / sum);
    }

    return n_taps;
}

size_t fir_filter_valid(const float *input, size_t n_rows, size_t n_bins,
                        const float *coeffs, size_t n_taps, float *output,
                        size_t max_output_rows) {

    if (input == NULL || coeffs == NULL || output == NULL || output == input ||
        output == coeffs || n_bins == 0 || n_taps == 0 || n_taps % 2 == 0 ||
        n_rows < n_taps || n_rows > SIZE_MAX / sizeof(float) / n_bins) {
        return 0;
    }

    /* 전체 계수가 입력 범위 안에 들어오는 위치만 계산한다. */
    size_t n_output = n_rows - n_taps + 1;

    if (max_output_rows < n_output) {
        return 0;
    }

    for (size_t g = 0; g < n_output; g++) {
        for (size_t k = 0; k < n_bins; k++) {

            double sum = 0.0;

            for (size_t j = 0; j < n_taps; j++) {
                /* 합성곱은 계수를 뒤집는다. Kaiser 대칭 계수는 뒤집어도 같다.
                 */
                sum += (double)coeffs[n_taps - 1 - j] *
                       input[(g + j) * n_bins + k];
            }

            output[g * n_bins + k] = (float)sum;
        }
    }

    return n_output;
}
