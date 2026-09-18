/* Port of the preserved wifi_csi_backup phase path; see docs/phase.md.
 * All spectral operations use double precision. No respiration labels enter
 * this file, and no amplitude result participates in the decision. */
#include "csi_resp/phase.h"
#include "csi_resp/csi_utils.h"
#include "csi_resp/filters.h"
#include <complex.h>
#include <float.h>
#include <limits.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// 저장 용량은 프로토콜 상한, 실제 계산 크기는 호출별 설정에서 구한다.
typedef struct {
    const csi_phase_config_t *config;
    int n_columns, n_frequencies, n_null;
    int raw_bins[CSI_MAX_SUBCARRIERS];
} phase_parameters;

csi_phase_config_t csi_phase_default_config(void) {
    csi_phase_config_t config = {.n_bins = 117,
                                 .subcarrier_spacing_hz = 312500.,
                                 .min_hz = .05,
                                 .max_hz = 1.,
                                 .frequency_step_hz = .005,
                                 .top_columns = 5,
                                 .whiten_bins = 61,
                                 .max_gap_s = .1,
                                 .min_duration_s = 20,
                                 .max_null_ratio = .05,
                                 .min_sharpness = 1.55,
                                 .max_agreement_bpm = 3.};

    for (size_t k = 0; k < config.n_bins; ++k) {
        config.bins[k].frequency_hz =
            ((double)k - 58) * config.subcarrier_spacing_hz;
        config.bins[k].role =
            k >= 57 && k <= 59 ? CSI_PHASE_BIN_NULL : CSI_PHASE_BIN_USED;
    }

    return config;
}

static int phase_config_valid(const csi_phase_config_t *c) {
    if (!c || c->n_bins == 0 || c->n_bins > CSI_MAX_SUBCARRIERS ||
        !isfinite(c->subcarrier_spacing_hz) || c->subcarrier_spacing_hz <= 0 ||
        !isfinite(c->min_hz) || !isfinite(c->max_hz) ||
        !isfinite(c->frequency_step_hz) || c->min_hz <= 0 ||
        c->min_hz >= c->max_hz || c->frequency_step_hz <= 0 ||
        c->top_columns == 0 || c->top_columns > CSI_MAX_SUBCARRIERS ||
        c->whiten_bins == 0 || c->whiten_bins % 2 == 0 ||
        c->whiten_bins > INT_MAX / 2) {
        return 0;
    }

    return isfinite(c->max_gap_s) && c->max_gap_s > 0 &&
           isfinite(c->min_duration_s) && c->min_duration_s > 0 &&
           isfinite(c->max_null_ratio) && c->max_null_ratio >= 0 &&
           isfinite(c->min_sharpness) && c->min_sharpness >= 0 &&
           isfinite(c->max_agreement_bpm) && c->max_agreement_bpm > 0;
}

static int phase_parameters_init(phase_parameters *p,
                                 const csi_phase_config_t *c) {
    if (!phase_config_valid(c)) {
        return 1;
    }

    memset(p, 0, sizeof(*p));
    p->config = c;

    for (size_t k = 0; k < c->n_bins; ++k) {
        if (c->bins[k].role == CSI_PHASE_BIN_USED) {
            if (!isfinite(c->bins[k].frequency_hz) ||
                !isfinite(2 * CSI_PI * c->bins[k].frequency_hz)) {
                return 1;
            }

            // 배치는 주파수 오름차순으로 지정한다. 중복/역순은 거부한다.
            if (p->n_columns &&
                c->bins[k].frequency_hz <=
                    c->bins[p->raw_bins[p->n_columns - 1]].frequency_hz) {
                return 1;
            }

            p->raw_bins[p->n_columns++] = (int)k;
        } else if (c->bins[k].role == CSI_PHASE_BIN_NULL) {
            ++p->n_null;
        } else if (c->bins[k].role != CSI_PHASE_BIN_SKIP) {
            return 1;
        }
    }

    double steps = (c->max_hz - c->min_hz) / c->frequency_step_hz;
    double nearest = nearbyint(steps);

    if (fabs(steps - nearest) <= 8 * DBL_EPSILON * fmax(1, fabs(steps))) {
        steps = nearest;
    }

    if (!isfinite(steps) || steps < 1 || steps >= INT_MAX / 2 ||
        steps >= SIZE_MAX / sizeof(double) - 1 || p->n_columns < 2 ||
        c->top_columns > (size_t)p->n_columns) {
        return 1;
    }

    p->n_frequencies = (int)floor(steps) + 1;
    return 0;
}

static int raw_bin(int k, const phase_parameters *p) {
    return p->raw_bins[k];
}

static double omega(int k, const phase_parameters *p) {
    return 2 * CSI_PI * p->config->bins[raw_bin(k, p)].frequency_hz;
}

static double power(double complex z) {
    return creal(z) * creal(z) + cimag(z) * cimag(z);
}

static double wrap_angle(double a) {
    return a - 2 * CSI_PI * floor((a + CSI_PI) / (2 * CSI_PI));
}

/* Bluestein chirp convolution: exact requested frequencies, without padding
 * the signal's FFT grid. The radix-2 padding is only for the convolution.
 * Kernel FFT is reused across the configured columns. */
typedef struct {
    size_t n, m, length;
    double complex *kernel, *work, *input_chirp, *output_chirp;
} chirp_plan;

static void chirp_free(chirp_plan *p) {
    free(p->kernel);
    free(p->work);
    free(p->input_chirp);
    free(p->output_chirp);
    memset(p, 0, sizeof(*p));
}

static int chirp_init(chirp_plan *p, size_t n, size_t m, double start,
                      double step) {
    if (!n || !m || n > SIZE_MAX - m) {
        return 1;
    }

    p->n = n;
    p->m = m;
    p->length = 1;

    while (p->length < n + m - 1) {
        if (p->length > SIZE_MAX / 2) {
            return 1;
        }

        p->length *= 2;
    }

    if (p->length > SIZE_MAX / sizeof(double complex)) {
        return 1;
    }

    p->kernel = calloc(p->length, sizeof(double complex));
    p->work = malloc(p->length * sizeof(double complex));
    p->input_chirp = malloc(n * sizeof(double complex));
    p->output_chirp = malloc(m * sizeof(double complex));

    if (!p->kernel || !p->work || !p->input_chirp || !p->output_chirp) {
        chirp_free(p);

        return 1;
    }

    for (size_t j = 0; j < n; ++j) {
        double square = (double)j * j;
        p->input_chirp[j] = cexp(-I * CSI_PI * (step * square + 2 * start * j));

        if (j) {
            p->kernel[p->length - j] = cexp(I * CSI_PI * step * square);
        }
    }

    for (size_t j = 0; j < m; ++j) {
        p->output_chirp[j] = cexp(-I * CSI_PI * step * (double)j * j);
        p->kernel[j] = conj(p->output_chirp[j]);
    }

    csi_fft_complex(p->kernel, p->length);

    return 0;
}

static void chirp_apply(chirp_plan *p, const double complex *x, size_t stride) {
    memset(p->work, 0, p->length * sizeof(*p->work));

    double complex mean = 0;

    for (size_t i = 0; i < p->n; ++i) {
        mean += x[i * stride];
    }

    mean /= p->n;

    for (size_t i = 0; i < p->n; ++i) {
        p->work[i] = (x[i * stride] - mean) * p->input_chirp[i];
    }

    csi_fft_complex(p->work, p->length);

    for (size_t i = 0; i < p->length; ++i) {
        p->work[i] *= p->kernel[i];
    }

    csi_ifft_complex(p->work, p->length);

    for (size_t i = 0; i < p->m; ++i) {
        p->work[i] *= p->output_chirp[i];
    }
}

/* RMS normalization then legacy Algorithm 4 (coarse + static target + WLS).
 * Neighbour unwrap operates on the retained subcarrier list, as in Python. */
// RMS 정규화와 초기 위상 보정으로 정적 기준을 만든다.
static void build_phase_target(double complex *h, size_t n, double *tau,
                               double complex target[CSI_MAX_SUBCARRIERS],
                               const phase_parameters *p) {
    for (size_t i = 0; i < n; ++i) {
        double complex *row = h + i * p->n_columns, corr = 0, sum = 0;
        double rms = 0;

        for (int k = 0; k < p->n_columns; ++k) {
            rms += power(row[k]);
        }

        rms = sqrt(rms / p->n_columns);

        if (rms == 0) {
            rms = 1;
        }

        for (int k = 0; k < p->n_columns; ++k) {
            row[k] /= rms;
        }

        for (int k = 0; k < p->n_columns - 1; ++k) {
            corr += row[k] * conj(row[k + 1]);
        }

        tau[i] =
            (1 / p->config->subcarrier_spacing_hz) / (2 * CSI_PI) * carg(corr);

        for (int k = 0; k < p->n_columns; ++k) {
            sum += row[k] * cexp(I * omega(k, p) * tau[i]);
        }

        double psi = -carg(sum);

        for (int k = 0; k < p->n_columns; ++k) {
            target[k] += row[k] * cexp(I * (omega(k, p) * tau[i] + psi)) / n;
        }
    }
}

static int
select_phase_columns(const double complex target[CSI_MAX_SUBCARRIERS],
                     int keep[CSI_MAX_SUBCARRIERS], const phase_parameters *p) {
    double scale = 0;

    for (int k = 0; k < p->n_columns; ++k) {
        scale += power(target[k]) / p->n_columns;
    }

    int nk = 0;

    for (int k = 0; k < p->n_columns; ++k) {
        if (scale <= 0 || power(target[k]) > .1 * scale) {
            keep[nk++] = k;
        }
    }

    if (nk < 8) {
        nk = p->n_columns;

        for (int k = 0; k < p->n_columns; ++k) {
            keep[k] = k;
        }
    }

    return nk;
}

static void fit_phase_offsets(const double complex *w, const int *keep, int nk,
                              double *slope, double *intercept,
                              const phase_parameters *p) {
    double y[CSI_MAX_SUBCARRIERS], weights[CSI_MAX_SUBCARRIERS],
        prev = 0, offset = 0, sw = 0, mx = 0, my = 0;

    for (int j = 0; j < nk; ++j) {
        double complex ref = 0;

        for (int q = (j > 3 ? j - 3 : 0); q < nk && q <= j + 3; ++q) {
            ref += w[q];
        }

        double angle = carg(ref);

        if (j) {
            double delta = angle - prev;

            if (fabs(delta) > CSI_PI) {
                offset += wrap_angle(delta) - delta;
            }
        }

        prev = angle;
        angle += offset;
        y[j] = wrap_angle(carg(w[j]) - angle) + angle;
        weights[j] = cabs(w[j]);
        sw += weights[j];
        mx += weights[j] * omega(keep[j], p);
        my += weights[j] * y[j];
    }

    *slope = 0;
    *intercept = 0;

    if (sw > 0) {
        mx /= sw;
        my /= sw;

        double variance = 0, covariance = 0;

        for (int j = 0; j < nk; ++j) {
            double d = omega(keep[j], p) - mx;
            variance += weights[j] * d * d;
            covariance += weights[j] * d * (y[j] - my);
        }

        if (variance > 0) {
            *slope = covariance / variance;
        }

        *intercept = my - *slope * mx;
    }
}

static void correct_phase_row(double complex *row, double tau,
                              const double complex *target, const int *keep,
                              int nk, const phase_parameters *p) {
    double complex w[CSI_MAX_SUBCARRIERS];
    for (int j = 0; j < nk; ++j) {
        int k = keep[j];
        w[j] = conj(row[k]) * target[k] * cexp(-I * omega(k, p) * tau);
    }

    double slope, intercept;
    fit_phase_offsets(w, keep, nk, &slope, &intercept, p);

    for (int k = 0; k < p->n_columns; ++k) {
        row[k] *= cexp(I * (omega(k, p) * (tau + slope) + intercept));
    }
}

static int correct_phase(double complex *h, size_t n,
                         const phase_parameters *p) {
    double *tau = malloc(n * sizeof(*tau));
    double complex target[CSI_MAX_SUBCARRIERS] = {0};
    int keep[CSI_MAX_SUBCARRIERS];

    if (!tau) {
        return 1;
    }

    build_phase_target(h, n, tau, target, p);
    int nk = select_phase_columns(target, keep, p);

    for (size_t i = 0; i < n; ++i) {
        correct_phase_row(h + i * p->n_columns, tau[i], target, keep, nk, p);
    }

    free(tau);
    return 0;
}

typedef struct {
    size_t index;
    double height;
    int keep;
} peak;

static int compare_peak(const void *a, const void *b) {
    const peak *x = a, *y = b;

    if (x->height != y->height) {
        return x->height > y->height ? -1 : 1;
    }

    return x->index > y->index ? -1 : x->index < y->index;
}

static size_t find_local_peaks(const double *x, size_t n, peak *peaks) {
    size_t np = 0;

    for (size_t i = 1; i + 1 < n; ++i) {
        if (x[i] > x[i - 1]) {
            size_t end = i;

            while (end + 1 < n && x[end + 1] == x[i]) {
                ++end;
            }

            if (end + 1 < n && x[end] > x[end + 1]) {
                peaks[np++] = (peak){(i + end) / 2, x[i], 1};
            }

            i = end;
        }
    }

    return np;
}

static void suppress_close_peaks(peak *peaks, size_t np, double fs,
                                 const phase_parameters *p) {
    qsort(peaks, np, sizeof(*peaks), compare_peak);

    double distance = fmax(1, nearbyint(fs / p->config->max_hz));

    for (size_t i = 0; i < np; ++i) {
        if (peaks[i].keep) {
            for (size_t j = i + 1; j < np; ++j) {
                size_t a = peaks[i].index, b = peaks[j].index;

                if ((a > b ? a - b : b - a) < distance) {
                    peaks[j].keep = 0;
                }
            }
        }
    }
}

static double peak_threshold(const double *x, size_t n) {
    double mean = 0, variance = 0;

    for (size_t i = 0; i < n; ++i) {
        mean += x[i] / n;
    }

    for (size_t i = 0; i < n; ++i) {
        variance += (x[i] - mean) * (x[i] - mean) / n;
    }

    return .3 * sqrt(variance);
}

static double peak_prominence(const double *x, size_t n, size_t k) {
    double left = x[k], right = x[k];

    for (size_t i = k; i > 0;) {
        --i;

        if (x[i] > x[k]) {
            break;
        }

        left = fmin(left, x[i]);
    }

    for (size_t i = k + 1; i < n; ++i) {
        if (x[i] > x[k]) {
            break;
        }

        right = fmin(right, x[i]);
    }

    return x[k] - fmax(left, right);
}

static int count_peaks(const double *x, size_t n, double fs, size_t *count,
                       double *bpm, const phase_parameters *p) {
    peak *peaks = malloc(n * sizeof(*peaks));

    if (!peaks) {
        return 1;
    }

    size_t np = find_local_peaks(x, n, peaks);

    suppress_close_peaks(peaks, np, fs, p);

    double threshold = peak_threshold(x, n);
    size_t first = n, last = 0;
    *count = 0;
    *bpm = 0;

    for (size_t j = 0; j < np; ++j) {
        if (peaks[j].keep) {
            size_t k = peaks[j].index;
            if (peak_prominence(x, n, k) < threshold) {
                continue;
            }

            ++*count;

            if (k < first) {
                first = k;
            }

            if (k > last) {
                last = k;
            }
        }
    }

    if (*count >= 2) {
        *bpm = (*count - 1) * fs / (last - first) * 60;
    }

    free(peaks);

    return 0;
}

static double remove_phase_mean(double complex *h, size_t n,
                                const phase_parameters *p) {
    double maximum = 0;

    for (int k = 0; k < p->n_columns; ++k) {
        double complex mean = 0;

        for (size_t i = 0; i < n; ++i) {
            mean += h[i * p->n_columns + k] / n;
        }

        for (size_t i = 0; i < n; ++i) {
            h[i * p->n_columns + k] -= mean;
            maximum = fmax(maximum, cabs(h[i * p->n_columns + k]));
        }
    }

    return maximum;
}

static int phase_band_energy(const double complex *h, size_t n, double fs,
                             double energies[CSI_MAX_SUBCARRIERS],
                             const phase_parameters *p) {
    chirp_plan plan = {0};

    if (chirp_init(&plan, n, n, 0, 1.0 / n)) {
        return 1;
    }

    for (int k = 0; k < p->n_columns; ++k) {
        chirp_apply(&plan, h + k, p->n_columns);

        for (size_t j = 1; j < n; ++j) {
            double freq = (j <= n / 2 ? j : n - j) * fs / n;

            if (freq >= p->config->min_hz && freq <= p->config->max_hz) {
                energies[k] += power(plan.work[j]);
            }
        }
    }

    chirp_free(&plan);

    return 0;
}

static void combine_phase_columns(const double complex *h, size_t n,
                                  double *energies, double *wave, double *ref,
                                  int *selected_bins,
                                  const phase_parameters *p) {
    for (int j = 0; j < (int)p->config->top_columns; ++j) {
        int best = 0;

        for (int k = 1; k < p->n_columns; ++k) {
            if (energies[k] > energies[best]) {
                best = k;
            }
        }

        selected_bins[j] = raw_bin(best, p);
        energies[best] = -1;

        double complex second = 0;

        for (size_t i = 0; i < n; ++i) {
            second += h[i * p->n_columns + best] * h[i * p->n_columns + best];
        }

        double complex rotation = cexp(-I * .5 * carg(second));
        double dot = 0;

        if (!j) {
            for (size_t i = 0; i < n; ++i) {
                ref[i] = creal(h[i * p->n_columns + best] * rotation);
            }
        }

        for (size_t i = 0; i < n; ++i) {
            dot += ref[i] * creal(h[i * p->n_columns + best] * rotation);
        }

        double sign = dot < 0 ? -1 : 1;

        for (size_t i = 0; i < n; ++i) {
            wave[i] += sign * creal(h[i * p->n_columns + best] * rotation);
        }
    }
}

static int phase_spectrum(const double complex *h, size_t n, double fs,
                          double *spectrum, const phase_parameters *p) {
    chirp_plan plan = {0};

    if (chirp_init(&plan, n, p->n_frequencies, p->config->min_hz / fs,
                   p->config->frequency_step_hz / fs)) {
        return 1;
    }

    for (int k = 0; k < p->n_columns; ++k) {
        chirp_apply(&plan, h + k, p->n_columns);

        for (int j = 0; j < p->n_frequencies; ++j) {
            spectrum[j] += power(plan.work[j]);
        }
    }

    chirp_free(&plan);

    return 0;
}

static int select_spectral_peak(const double *spectrum, double *sharpness,
                                const phase_parameters *p) {
    double *background = malloc((size_t)p->n_frequencies * sizeof(*background));
    double *values = malloc(p->config->whiten_bins * sizeof(*values));

    if (!background || !values) {
        free(background);
        free(values);
        return -1;
    }

    double floor_power = 0;

    for (int j = 0; j < p->n_frequencies; ++j) {

        for (int q = -(int)(p->config->whiten_bins / 2);
             q <= (int)(p->config->whiten_bins / 2); ++q) {
            int index = j + q;

            if (index < 0) {
                index = 0;
            }

            if (index >= p->n_frequencies) {
                index = p->n_frequencies - 1;
            }

            values[q + p->config->whiten_bins / 2] = spectrum[index];
        }

        background[j] = csi_median(values, p->config->whiten_bins);
        floor_power = fmax(floor_power, background[j]);
    }

    floor_power = fmax(floor_power * 1e-6, DBL_MIN);

    int best = 0;

    for (int j = 0; j < p->n_frequencies; ++j) {
        double whitened = spectrum[j] / fmax(background[j], floor_power);

        if (whitened > *sharpness) {
            *sharpness = whitened;
            best = j;
        }
    }

    free(background);
    free(values);
    return best;
}

static void decide_phase_candidate(int best, csi_phase_result_t *r,
                                   const phase_parameters *p) {
    r->peak_fallback =
        (best == 0 || best == p->n_frequencies - 1) && r->peak_count_bpm > 0;
    r->bpm = r->peak_fallback ? r->peak_count_bpm : r->spectral_bpm;
    r->agreement_bpm = r->peak_count_bpm > 0
                           ? fabs(r->spectral_bpm - r->peak_count_bpm)
                           : INFINITY;

    if (r->sharpness <= p->config->min_sharpness) {
        r->reasons |= CSI_PHASE_WEAK;
    }

    if (r->agreement_bpm >= p->config->max_agreement_bpm) {
        r->reasons |= CSI_PHASE_DISAGREEMENT;
    }

    r->accepted = !r->reasons && r->bpm > 0;
}

// 각 단계의 계산은 위 함수들에 두고, 여기서는 분석 순서만 조립한다.
static int analyze(double complex *h, size_t n, double fs,
                   csi_phase_result_t *r, const phase_parameters *p) {
    double *spectrum = calloc((size_t)p->n_frequencies, sizeof(*spectrum));
    double energies[CSI_MAX_SUBCARRIERS] = {0};
    double *wave = calloc(n, sizeof(*wave));
    double *ref = malloc(n * sizeof(*ref));
    int status = 1;

    if (!wave || !ref || !spectrum) {
        goto done;
    }

    double maximum = remove_phase_mean(h, n, p);

    if (phase_band_energy(h, n, fs, energies, p)) {
        goto done;
    }

    combine_phase_columns(h, n, energies, wave, ref, r->selected_bins, p);

    if (hampel_filter(wave, n) ||
        butter_bandpass_zero_phase(wave, n, fs, p->config->min_hz,
                                   p->config->max_hz)) {
        goto done;
    }

    for (size_t i = 0; i < n; ++i) {
        maximum = fmax(maximum, fabs(wave[i]));
    }

    if (maximum <= 1e-12) {
        r->reasons = CSI_PHASE_FLAT;
        status = 0;
        goto done;
    }

    if (phase_spectrum(h, n, fs, spectrum, p)) {
        goto done;
    }

    int best = select_spectral_peak(spectrum, &r->sharpness, p);
    if (best < 0) {
        goto done;
    }

    r->spectral_bpm =
        (p->config->min_hz + p->config->frequency_step_hz * best) * 60;
    double f0 = r->spectral_bpm / 60;

    if (butter_bandpass_zero_phase(wave, n, fs,
                                   fmax(.7 * f0, p->config->min_hz),
                                   fmin(1.5 * f0, p->config->max_hz)) ||
        count_peaks(wave, n, fs, &r->n_peaks, &r->peak_count_bpm, p)) {
        goto done;
    }

    decide_phase_candidate(best, r, p);
    status = 0;

done:
    free(spectrum);
    free(wave);
    free(ref);
    return status;
}

static int inspect_phase_frame(const csi_frame_t *frame, double *null_sum,
                               double *occupied_sum,
                               int seen[CSI_MAX_SUBCARRIERS],
                               const phase_parameters *p) {
    int valid = 1;
    int used_column = 0;

    for (int k = 0; k < (int)frame->n; ++k) {
        csi_phase_bin_role_t role = p->config->bins[k].role;

        if (role == CSI_PHASE_BIN_SKIP) {
            continue;
        }

        double re = frame->re[k], im = frame->im[k];

        if (!isfinite(re) || !isfinite(im)) {
            return -1;
        }

        if (role == CSI_PHASE_BIN_NULL) {
            *null_sum += hypot(re, im);
        } else {
            *occupied_sum += hypot(re, im);

            if (!frame->valid[k]) {
                valid = 0;
            }

            if (frame->valid[k] && (re != 0 || im != 0)) {
                seen[used_column] = 1;
            }

            ++used_column;
        }
    }

    return valid;
}

static int collect_phase_rows(const csi_frame_t *frames, size_t count,
                              double *times, double *intervals,
                              size_t *n_retained, double *max_gap_s,
                              const phase_parameters *p) {
    size_t retained = 0;

    uint64_t elapsed = 0;
    double null_sum = 0, occupied_sum = 0;
    int seen[CSI_MAX_SUBCARRIERS] = {0};

    for (size_t i = 0; i < count; ++i) {
        if (frames[i].n != p->config->n_bins) {
            return 1;
        }

        if (i) {
            uint32_t delta =
                frames[i].meta.timestamp - frames[i - 1].meta.timestamp;

            if (delta >= UINT32_C(0x80000000)) {
                return 1;
            }

            elapsed += delta;

            if (elapsed > UINT32_MAX) {
                return 1;
            }
        }

        int valid =
            inspect_phase_frame(&frames[i], &null_sum, &occupied_sum, seen, p);

        if (valid < 0) {
            return 1;
        }

        if (valid) {
            times[retained] = elapsed / 1e6;
            intervals[retained++] = (double)i;
        }
    }

    if (retained < 2 || occupied_sum <= 0 ||
        (p->n_null > 0 &&
         (null_sum / p->n_null) / (occupied_sum / p->n_columns) >
             p->config->max_null_ratio)) {
        return 1;
    }

    for (int k = 0; k < p->n_columns; ++k) {
        if (!seen[k]) {
            return 1;
        }
    }

    *max_gap_s = fmax(times[0], elapsed / 1e6 - times[retained - 1]);
    *n_retained = retained;
    return 0;
}

static void interpolate_phase(const double complex *h, const double *times,
                              size_t retained, size_t n, double fs,
                              double complex *uniform,
                              const phase_parameters *p) {
    size_t left = 0;

    for (size_t i = 0; i < n; ++i) {
        double t = times[0] + i / fs;

        while (left + 1 < retained && times[left + 1] <= t) {
            ++left;
        }

        size_t right = left + 1 < retained ? left + 1 : left;
        double fraction =
            right == left ? 0
                          : (t - times[left]) / (times[right] - times[left]);

        for (int k = 0; k < p->n_columns; ++k) {
            uniform[i * p->n_columns + k] =
                h[left * p->n_columns + k] +
                fraction *
                    (h[right * p->n_columns + k] - h[left * p->n_columns + k]);
        }
    }
}

static int prepare_phase_window(const csi_frame_t *frames, size_t count,
                                double *times, double *intervals,
                                double complex **samples, csi_phase_result_t *r,
                                const phase_parameters *p) {
    size_t retained = 0;
    double complex *h = NULL;

    if (collect_phase_rows(frames, count, times, intervals, &retained,
                           &r->max_gap_s, p)) {
        return 1;
    }

    h = malloc(retained * p->n_columns * sizeof(*h));

    *samples = h;

    if (!h) {
        return 1;
    }

    for (size_t j = 0; j < retained; ++j) {
        size_t i = (size_t)intervals[j];

        for (int k = 0; k < p->n_columns; ++k) {
            h[j * p->n_columns + k] =
                frames[i].re[raw_bin(k, p)] + I * frames[i].im[raw_bin(k, p)];
        }

        if (j) {
            r->max_gap_s = fmax(r->max_gap_s, times[j] - times[j - 1]);
        }
    }

    if (r->max_gap_s > p->config->max_gap_s + 1e-9 ||
        times[retained - 1] - times[0] < p->config->min_duration_s) {
        return 1;
    }

    for (size_t i = 1; i < retained; ++i) {
        intervals[i - 1] = times[i] - times[i - 1];
    }

    double dt = csi_median(intervals, retained - 1);

    if (!(dt > 0) || 1 / dt <= 2 * p->config->max_hz / .99) {
        return 1;
    }

    double fs = 1 / dt,
           grid_count = floor((times[retained - 1] - times[0]) * fs) + 1;

    if (grid_count > SIZE_MAX / (p->n_columns * sizeof(double complex))) {
        return 1;
    }

    size_t n = (size_t)grid_count;

    r->n_samples = n;
    r->n_retained = retained;
    r->sample_rate_hz = fs;
    r->start_s = times[0];
    r->duration_s = (n - 1) / fs;
    return 0;
}

int csi_phase_process_with_config(const csi_frame_t *frames, size_t count,
                                  const csi_phase_config_t *config,
                                  csi_phase_result_t *r) {
    phase_parameters parameters;
    const phase_parameters *p = &parameters;

    if (phase_parameters_init(&parameters, config)) {
        return 1;
    }

    if (!frames || !r || count < 2 ||
        count > SIZE_MAX / (p->n_columns * sizeof(double complex))) {
        return 1;
    }

    memset(r, 0, sizeof(*r));
    r->n_selected = config->top_columns;
    r->n_columns = p->n_columns;
    r->n_frequencies = p->n_frequencies;

    for (int j = 0; j < (int)p->config->top_columns; ++j) {
        r->selected_bins[j] = -1;
    }

    double *times = malloc(count * sizeof(*times)),
           *intervals = malloc(count * sizeof(*intervals));

    double complex *h = NULL, *uniform = NULL;

    int status = 1;

    if (!times || !intervals) {
        goto done;
    }

    if (prepare_phase_window(frames, count, times, intervals, &h, r, p)) {
        goto done;
    }

    size_t retained = r->n_retained, n = r->n_samples;
    double fs = r->sample_rate_hz;

    if (correct_phase(h, retained, p)) {
        goto done;
    }

    uniform = malloc(n * p->n_columns * sizeof(*uniform));

    if (!uniform) {
        goto done;
    }

    interpolate_phase(h, times, retained, n, fs, uniform, p);

    free(h);

    h = NULL;
    status = analyze(uniform, n, fs, r, p);

done:
    free(h);
    free(uniform);
    free(times);
    free(intervals);

    return status;
}

int csi_phase_process(const csi_frame_t *frames, size_t count,
                      csi_phase_result_t *r) {
    csi_phase_config_t config = csi_phase_default_config();
    return csi_phase_process_with_config(frames, count, &config, r);
}
