/* Port of the preserved wifi_csi_backup phase path; see docs/phase.md.
 * All spectral operations use double precision. No respiration labels enter
 * this file, and no amplitude result participates in the decision. */
#include "csi_resp/phase.h"
#include "csi_resp/csi_utils.h"
#include "csi_resp/filters.h"
#include <complex.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define COLS 114
#define FREQS 191

static int raw_bin(int k) {
    return k < 57 ? k : k + 3;
}

static double omega(int k) {
    return 2 * CSI_PI * (raw_bin(k) - 58) * 312500.;
}

static double power(double complex z) {
    return creal(z) * creal(z) + cimag(z) * cimag(z);
}

static double wrap_angle(double a) {
    return a - 2 * CSI_PI * floor((a + CSI_PI) / (2 * CSI_PI));
}

/* Bluestein chirp convolution: exact requested frequencies, without padding
 * the signal's FFT grid. The radix-2 padding is only for the convolution.
 * Kernel FFT is reused across the 114 columns. */
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
                               double complex target[COLS]) {
    for (size_t i = 0; i < n; ++i) {
        double complex *row = h + i * COLS, corr = 0, sum = 0;
        double rms = 0;

        for (int k = 0; k < COLS; ++k) {
            rms += power(row[k]);
        }

        rms = sqrt(rms / COLS);

        if (rms == 0) {
            rms = 1;
        }

        for (int k = 0; k < COLS; ++k) {
            row[k] /= rms;
        }

        for (int k = 0; k < COLS - 1; ++k) {
            corr += row[k] * conj(row[k + 1]);
        }

        tau[i] = 3.2e-6 / (2 * CSI_PI) * carg(corr);

        for (int k = 0; k < COLS; ++k) {
            sum += row[k] * cexp(I * omega(k) * tau[i]);
        }

        double psi = -carg(sum);

        for (int k = 0; k < COLS; ++k) {
            target[k] += row[k] * cexp(I * (omega(k) * tau[i] + psi)) / n;
        }
    }
}

static int select_phase_columns(const double complex target[COLS],
                                int keep[COLS]) {
    double scale = 0;

    for (int k = 0; k < COLS; ++k) {
        scale += power(target[k]) / COLS;
    }

    int nk = 0;

    for (int k = 0; k < COLS; ++k) {
        if (scale <= 0 || power(target[k]) > .1 * scale) {
            keep[nk++] = k;
        }
    }

    if (nk < 8) {
        nk = COLS;

        for (int k = 0; k < COLS; ++k) {
            keep[k] = k;
        }
    }

    return nk;
}

static void fit_phase_offsets(const double complex *w, const int *keep, int nk,
                              double *slope, double *intercept) {
    double y[COLS], weights[COLS], prev = 0, offset = 0, sw = 0, mx = 0, my = 0;

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
        mx += weights[j] * omega(keep[j]);
        my += weights[j] * y[j];
    }

    *slope = 0;
    *intercept = 0;

    if (sw > 0) {
        mx /= sw;
        my /= sw;

        double variance = 0, covariance = 0;

        for (int j = 0; j < nk; ++j) {
            double d = omega(keep[j]) - mx;
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
                              int nk) {
    double complex w[COLS];
    for (int j = 0; j < nk; ++j) {
        int k = keep[j];
        w[j] = conj(row[k]) * target[k] * cexp(-I * omega(k) * tau);
    }

    double slope, intercept;
    fit_phase_offsets(w, keep, nk, &slope, &intercept);

    for (int k = 0; k < COLS; ++k) {
        row[k] *= cexp(I * (omega(k) * (tau + slope) + intercept));
    }
}

static int correct_phase(double complex *h, size_t n) {
    double *tau = malloc(n * sizeof(*tau));
    double complex target[COLS] = {0};
    int keep[COLS];

    if (!tau) {
        return 1;
    }

    build_phase_target(h, n, tau, target);
    int nk = select_phase_columns(target, keep);

    for (size_t i = 0; i < n; ++i) {
        correct_phase_row(h + i * COLS, tau[i], target, keep, nk);
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

static void suppress_close_peaks(peak *peaks, size_t np, double fs) {
    qsort(peaks, np, sizeof(*peaks), compare_peak);

    size_t distance = (size_t)fmax(1, nearbyint(fs));

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
                       double *bpm) {
    peak *peaks = malloc(n * sizeof(*peaks));

    if (!peaks) {
        return 1;
    }

    size_t np = find_local_peaks(x, n, peaks);

    suppress_close_peaks(peaks, np, fs);

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

static double remove_phase_mean(double complex *h, size_t n) {
    double maximum = 0;

    for (int k = 0; k < COLS; ++k) {
        double complex mean = 0;

        for (size_t i = 0; i < n; ++i) {
            mean += h[i * COLS + k] / n;
        }

        for (size_t i = 0; i < n; ++i) {
            h[i * COLS + k] -= mean;
            maximum = fmax(maximum, cabs(h[i * COLS + k]));
        }
    }

    return maximum;
}

static int phase_band_energy(const double complex *h, size_t n, double fs,
                             double energies[COLS]) {
    chirp_plan plan = {0};

    if (chirp_init(&plan, n, n, 0, 1.0 / n)) {
        return 1;
    }

    for (int k = 0; k < COLS; ++k) {
        chirp_apply(&plan, h + k, COLS);

        for (size_t j = 1; j < n; ++j) {
            double freq = (j <= n / 2 ? j : n - j) * fs / n;

            if (freq >= .05 && freq <= 1.) {
                energies[k] += power(plan.work[j]);
            }
        }
    }

    chirp_free(&plan);

    return 0;
}

static void combine_phase_columns(const double complex *h, size_t n,
                                  double *energies, double *wave, double *ref,
                                  int selected_bins[5]) {
    for (int j = 0; j < 5; ++j) {
        int best = 0;

        for (int k = 1; k < COLS; ++k) {
            if (energies[k] > energies[best]) {
                best = k;
            }
        }

        selected_bins[j] = raw_bin(best);
        energies[best] = -1;

        double complex second = 0;

        for (size_t i = 0; i < n; ++i) {
            second += h[i * COLS + best] * h[i * COLS + best];
        }

        double complex rotation = cexp(-I * .5 * carg(second));
        double dot = 0;

        if (!j) {
            for (size_t i = 0; i < n; ++i) {
                ref[i] = creal(h[i * COLS + best] * rotation);
            }
        }

        for (size_t i = 0; i < n; ++i) {
            dot += ref[i] * creal(h[i * COLS + best] * rotation);
        }

        double sign = dot < 0 ? -1 : 1;

        for (size_t i = 0; i < n; ++i) {
            wave[i] += sign * creal(h[i * COLS + best] * rotation);
        }
    }
}

static int phase_spectrum(const double complex *h, size_t n, double fs,
                          double spectrum[FREQS]) {
    chirp_plan plan = {0};

    if (chirp_init(&plan, n, FREQS, .05 / fs, .005 / fs)) {
        return 1;
    }

    for (int k = 0; k < COLS; ++k) {
        chirp_apply(&plan, h + k, COLS);

        for (int j = 0; j < FREQS; ++j) {
            spectrum[j] += power(plan.work[j]);
        }
    }

    chirp_free(&plan);

    return 0;
}

static int select_spectral_peak(const double spectrum[FREQS],
                                double *sharpness) {
    double background[FREQS];

    double floor_power = 0;

    for (int j = 0; j < FREQS; ++j) {
        double values[61];

        for (int q = -30; q <= 30; ++q) {
            int index = j + q;

            if (index < 0) {
                index = 0;
            }

            if (index >= FREQS) {
                index = FREQS - 1;
            }

            values[q + 30] = spectrum[index];
        }

        background[j] = csi_median(values, 61);
        floor_power = fmax(floor_power, background[j]);
    }

    floor_power = fmax(floor_power * 1e-6, DBL_MIN);

    int best = 0;

    for (int j = 0; j < FREQS; ++j) {
        double whitened = spectrum[j] / fmax(background[j], floor_power);

        if (whitened > *sharpness) {
            *sharpness = whitened;
            best = j;
        }
    }

    return best;
}

static void decide_phase_candidate(int best, csi_phase_result_t *r) {
    r->peak_fallback =
        (best == 0 || best == FREQS - 1) && r->peak_count_bpm > 0;
    r->bpm = r->peak_fallback ? r->peak_count_bpm : r->spectral_bpm;
    r->agreement_bpm = r->peak_count_bpm > 0
                           ? fabs(r->spectral_bpm - r->peak_count_bpm)
                           : INFINITY;

    if (r->sharpness <= 1.55) {
        r->reasons |= CSI_PHASE_WEAK;
    }

    if (r->agreement_bpm >= 3) {
        r->reasons |= CSI_PHASE_DISAGREEMENT;
    }

    r->accepted = !r->reasons && r->bpm > 0;
}

// 각 단계의 계산은 위 함수들에 두고, 여기서는 분석 순서만 조립한다.
static int analyze(double complex *h, size_t n, double fs,
                   csi_phase_result_t *r) {
    double spectrum[FREQS] = {0}, energies[COLS] = {0};
    double *wave = calloc(n, sizeof(*wave));
    double *ref = malloc(n * sizeof(*ref));
    int status = 1;

    if (!wave || !ref) {
        goto done;
    }

    double maximum = remove_phase_mean(h, n);

    if (phase_band_energy(h, n, fs, energies)) {
        goto done;
    }

    combine_phase_columns(h, n, energies, wave, ref, r->selected_bins);

    if (hampel_filter(wave, n) ||
        butter_bandpass_zero_phase(wave, n, fs, .05, 1.)) {
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

    if (phase_spectrum(h, n, fs, spectrum)) {
        goto done;
    }

    int best = select_spectral_peak(spectrum, &r->sharpness);
    r->spectral_bpm = (.05 + .005 * best) * 60;
    double f0 = r->spectral_bpm / 60;

    if (butter_bandpass_zero_phase(wave, n, fs, fmax(.7 * f0, .05),
                                   fmin(1.5 * f0, 1.)) ||
        count_peaks(wave, n, fs, &r->n_peaks, &r->peak_count_bpm)) {
        goto done;
    }

    decide_phase_candidate(best, r);
    status = 0;

done:
    free(wave);
    free(ref);
    return status;
}

static int inspect_phase_frame(const csi_frame_t *frame, double *null_sum,
                               double *occupied_sum, int seen[COLS]) {
    int valid = 1;

    for (int k = 0; k < 117; ++k) {
        double re = frame->re[k], im = frame->im[k];

        if (!isfinite(re) || !isfinite(im)) {
            return -1;
        }

        if (k >= 57 && k <= 59) {
            *null_sum += hypot(re, im);
        } else {
            *occupied_sum += hypot(re, im);

            if (!frame->valid[k]) {
                valid = 0;
            }

            if (frame->valid[k] && (re != 0 || im != 0)) {
                seen[k < 57 ? k : k - 3] = 1;
            }
        }
    }

    return valid;
}

static int collect_phase_rows(const csi_frame_t *frames, size_t count,
                              double *times, double *intervals,
                              size_t *n_retained, double *max_gap_s) {
    size_t retained = 0;

    uint64_t elapsed = 0;
    double null_sum = 0, occupied_sum = 0;
    int seen[COLS] = {0};

    for (size_t i = 0; i < count; ++i) {
        if (frames[i].n != 117) {
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
            inspect_phase_frame(&frames[i], &null_sum, &occupied_sum, seen);

        if (valid < 0) {
            return 1;
        }

        if (valid) {
            times[retained] = elapsed / 1e6;
            intervals[retained++] = (double)i;
        }
    }

    if (retained < 2 || occupied_sum <= 0 ||
        (null_sum / 3) / (occupied_sum / COLS) > .05) {
        return 1;
    }

    for (int k = 0; k < COLS; ++k) {
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
                              double complex *uniform) {
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

        for (int k = 0; k < COLS; ++k) {
            uniform[i * COLS + k] =
                h[left * COLS + k] +
                fraction * (h[right * COLS + k] - h[left * COLS + k]);
        }
    }
}

static int prepare_phase_window(const csi_frame_t *frames, size_t count,
                                double *times, double *intervals,
                                double complex **samples,
                                csi_phase_result_t *r) {
    size_t retained = 0;
    double complex *h = NULL;

    if (collect_phase_rows(frames, count, times, intervals, &retained,
                           &r->max_gap_s)) {
        return 1;
    }

    h = malloc(retained * COLS * sizeof(*h));

    *samples = h;

    if (!h) {
        return 1;
    }

    for (size_t j = 0; j < retained; ++j) {
        size_t i = (size_t)intervals[j];

        for (int k = 0; k < COLS; ++k) {
            h[j * COLS + k] =
                frames[i].re[raw_bin(k)] + I * frames[i].im[raw_bin(k)];
        }

        if (j) {
            r->max_gap_s = fmax(r->max_gap_s, times[j] - times[j - 1]);
        }
    }

    if (r->max_gap_s > .1 + 1e-9 || times[retained - 1] - times[0] < 20) {
        return 1;
    }

    for (size_t i = 1; i < retained; ++i) {
        intervals[i - 1] = times[i] - times[i - 1];
    }

    double dt = csi_median(intervals, retained - 1);

    if (!(dt > 0) || 1 / dt <= 2 / .99) {
        return 1;
    }

    double fs = 1 / dt,
           grid_count = floor((times[retained - 1] - times[0]) * fs) + 1;

    if (grid_count > SIZE_MAX / (COLS * sizeof(double complex))) {
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

int csi_phase_process(const csi_frame_t *frames, size_t count,
                      csi_phase_result_t *r) {
    if (!frames || !r || count < 2 ||
        count > SIZE_MAX / (COLS * sizeof(double complex))) {
        return 1;
    }

    memset(r, 0, sizeof(*r));

    for (int j = 0; j < 5; ++j) {
        r->selected_bins[j] = -1;
    }

    double *times = malloc(count * sizeof(*times)),
           *intervals = malloc(count * sizeof(*intervals));

    double complex *h = NULL, *uniform = NULL;

    int status = 1;

    if (!times || !intervals) {
        goto done;
    }

    if (prepare_phase_window(frames, count, times, intervals, &h, r)) {
        goto done;
    }

    size_t retained = r->n_retained, n = r->n_samples;
    double fs = r->sample_rate_hz;

    if (correct_phase(h, retained)) {
        goto done;
    }

    uniform = malloc(n * COLS * sizeof(*uniform));

    if (!uniform) {
        goto done;
    }

    interpolate_phase(h, times, retained, n, fs, uniform);

    free(h);

    h = NULL;
    status = analyze(uniform, n, fs, r);

done:
    free(h);
    free(uniform);
    free(times);
    free(intervals);

    return status;
}
