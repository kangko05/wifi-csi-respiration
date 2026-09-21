#include "csi_resp/amplitude.h"
#include "csi_resp/csi_record.h"
#include "csi_resp/csi_utils.h"
#include "csi_resp/filters.h"
#include <float.h>
#include <limits.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

// csi 프레임 배열을 받아 첫 값을 0부터 시작하도록 보정
static uint32_t rel_time(const csi_frame_t *frames, size_t i) {
    return frames[i].meta.timestamp - frames[0].meta.timestamp;
}

// k번째 서브캐리어에서 다음 유효한 프레임을 찾는다.
static size_t next_valid(const csi_frame_t *frames, size_t n, uint16_t k,
                         size_t i) {
    while (i < n && !frames[i].valid[k]) {
        i++;
    }

    return i;
}

// k번째 서브캐리어를 보간에 써도 되는지 찾는다.
static int bin_eligible(const csi_frame_t *frames, size_t n, uint16_t k,
                        uint32_t *first, uint32_t *last) {
    size_t count = 0;
    uint32_t prev = 0;
    uint32_t max_gap = 0;

    for (size_t i = 0; i < n; i++) {
        // skip masked bin
        if (!frames[i].valid[k]) {
            continue;
        }

        uint32_t t = rel_time(frames, i);

        if (count == 0) {
            *first = t;
        }

        if (t - prev > max_gap) {
            max_gap = t - prev;
        }

        prev = t;
        count++;
    }

    uint32_t end = rel_time(frames, n - 1);

    if (end - prev > max_gap) {
        max_gap = end - prev;
    }

    *last = prev;

    return count >= CSI_INTERP_MIN_PACKETS &&
           (float)count / (float)n >= CSI_INTERP_MIN_FRACTION &&
           max_gap <= CSI_INTERP_MAX_GAP_US;
}

static void amp_fill_grid(const csi_frame_t *frames, size_t n_frames,
                          float *out, const uint8_t *eligible, size_t n_grid,
                          uint16_t n_bins, uint32_t start) {
    for (uint16_t k = 0; k < n_bins; k++) {
        if (!eligible[k]) {
            for (size_t g = 0; g < n_grid; g++) {
                out[g * n_bins + k] = 0.0f;
            }

            continue;
        }

        size_t lo = next_valid(frames, n_frames, k, 0);
        size_t hi = next_valid(frames, n_frames, k, lo + 1);

        for (size_t g = 0; g < n_grid; g++) {
            uint32_t x = start + (uint32_t)(g * CSI_INTERP_STEP_US);

            while (hi < n_frames && rel_time(frames, hi) <= x) {
                lo = hi;
                hi = next_valid(frames, n_frames, k, hi + 1);
            }

            uint32_t t_lo = rel_time(frames, lo);
            float a_lo = csi_abs(frames[lo].re[k], frames[lo].im[k]);

            if (hi >= n_frames || t_lo == x) {
                out[g * n_bins + k] = a_lo;
            } else {
                uint32_t t_hi = rel_time(frames, hi);
                float a_hi = csi_abs(frames[hi].re[k], frames[hi].im[k]);

                float frac = (float)(x - t_lo) / (float)(t_hi - t_lo);
                out[g * n_bins + k] = a_lo + frac * (a_hi - a_lo);
            }
        }
    }
}

static size_t csi_amp_interp(const csi_frame_t *frames, size_t n_frames,
                             float *out, uint8_t *eligible, size_t max_grid,
                             uint32_t *start_us) {
    if (frames == NULL || out == NULL || eligible == NULL || start_us == NULL ||
        n_frames < 2 || frames[0].n == 0 || frames[0].n > CSI_MAX_SUBCARRIERS) {
        return 0;
    }

    uint16_t n_bins = frames[0].n;

    for (size_t i = 1; i < n_frames; i++) {
        if (frames[i].n != n_bins ||
            rel_time(frames, i) < rel_time(frames, i - 1)) {
            return 0;
        }
    }

    uint32_t start = 0;
    uint32_t end = UINT32_MAX;
    uint16_t n_eligible = 0;

    for (uint16_t k = 0; k < n_bins; k++) {
        uint32_t first = 0;
        uint32_t last = 0;

        eligible[k] = bin_eligible(frames, n_frames, k, &first, &last);

        if (!eligible[k]) {
            continue;
        }

        n_eligible++;

        if (first > start) {
            start = first;
        }

        if (last < end) {
            end = last;
        }
    }

    if (n_eligible == 0 || end < start) {
        return 0;
    }

    size_t n_grid = (end - start) / CSI_INTERP_STEP_US + 1;

    if (n_grid > max_grid) {
        return 0;
    }

    *start_us = start;

    amp_fill_grid(frames, n_frames, out, eligible, n_grid, n_bins, start);

    return n_grid;
}

csi_amp_analysis_config_t csi_amp_default_analysis_config(void) {
    return (csi_amp_analysis_config_t){.min_hz = 0.05,
                                       .max_hz = 0.8,
                                       .min_duration_s = 20.0,
                                       .min_cycles = 3.0,
                                       .min_concentration = 0.35,
                                       .min_acf = 0.3,
                                       .agreement_resolution_bins = 1.0,
                                       .flat_relative_floor = 1e-8,
                                       .fft_oversampling = 4};
}

int csi_amp_init(csi_amp_context_t *context, const firwin_config_t *config) {
    if (context == NULL || config == NULL || context->coeffs != NULL) {
        return 1;
    }

    firwin_config_t actual = *config;
    actual.fs = 1000000.0f / CSI_INTERP_STEP_US;
    firwin_info_t info;

    if (!firwin_info(&actual, &info)) {
        return 1;
    }

    float *coeffs = malloc(sizeof(*coeffs) * info.n_taps);
    if (coeffs == NULL || firwin_coeff(&actual, coeffs, info.n_taps) == 0) {
        free(coeffs);
        return 1;
    }

    context->filter_info = info;
    context->coeffs = coeffs;
    context->analysis = csi_amp_default_analysis_config();

    return 0;
}

void csi_amp_free(csi_amp_context_t *context) {
    if (context != NULL) {
        free(context->coeffs);
        *context = (csi_amp_context_t){0};
    }
}

static size_t amp_downsample(float *filtered, size_t n_filtered, size_t n_bins,
                             size_t half, size_t down, uint32_t grid_start_us,
                             uint32_t *output_start_us,
                             uint64_t *output_step_us) {
    if (filtered == NULL || output_start_us == NULL || output_step_us == NULL ||
        down == 0 || n_bins == 0 ||
        n_filtered > SIZE_MAX / sizeof(float) / n_bins) {
        return 0;
    }

#if SIZE_MAX > UINT64_MAX / CSI_INTERP_STEP_US
    /* Only wider size_t targets can overflow the uint64_t output step. */
    if (down > UINT64_MAX / CSI_INTERP_STEP_US) {
        return 0;
    }
#endif

    size_t offset = (down - half % down) % down;

    if (offset >= n_filtered ||
        half > (UINT32_MAX - grid_start_us) / CSI_INTERP_STEP_US ||
        offset > (UINT32_MAX - grid_start_us) / CSI_INTERP_STEP_US - half) {
        return 0;
    }

    size_t n_output = (n_filtered - 1 - offset) / down + 1;

    for (size_t g = 0; g < n_output; g++) {
        size_t source = offset + g * down;
        memmove(&filtered[g * n_bins], &filtered[source * n_bins],
                n_bins * sizeof(*filtered));
    }

    *output_start_us =
        grid_start_us + (uint32_t)((half + offset) * CSI_INTERP_STEP_US);

    *output_step_us = (uint64_t)down * CSI_INTERP_STEP_US;

    return n_output;
}

/* PSD용 periodic Hann. Python periodogram(window="hann")과 같은 정의.
 * 윈도우 길이는 실제 데이터 길이이며 zero-padding 길이가 아니다. */
static void amp_hann(float *window, size_t n) {

    if (n == 1) {
        window[0] = 1.0f;
        return;
    }

    for (size_t g = 0; g < n; g++) {
        window[g] =
            (float)(0.5 - 0.5 * cos(2.0 * CSI_PI * (double)g / (double)n));
    }
}

static int amp_analysis_valid(const csi_amp_analysis_config_t *c, double fs) {
    return isfinite(fs) && fs > 0 && isfinite(c->min_hz) && c->min_hz > 0 &&
           isfinite(c->max_hz) && c->max_hz > c->min_hz &&
           c->max_hz < 0.4 * fs && isfinite(c->min_duration_s) &&
           c->min_duration_s > 0 && isfinite(c->min_cycles) &&
           c->min_cycles > 0 && isfinite(c->min_concentration) &&
           c->min_concentration > 0 && c->min_concentration <= 1 &&
           isfinite(c->min_acf) && c->min_acf > 0 && c->min_acf <= 1 &&
           isfinite(c->agreement_resolution_bins) &&
           c->agreement_resolution_bins > 0 &&
           isfinite(c->flat_relative_floor) && c->flat_relative_floor > 0 &&
           c->fft_oversampling > 0;
}

static int amp_float_compare(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return (x > y) - (x < y);
}

/* SciPy periodogram(scaling="density", return_onesided=True).
 * DC와 Nyquist 이외는 음수 주파수의 파워를 합쳐 2배로 만든다. */
static void amp_psd(const float *re, const float *im, size_t n_fft, double fs,
                    double window_power, double *psd) {

    for (size_t j = 0; j <= n_fft / 2; j++) {
        double power = (double)re[j] * re[j] + (double)im[j] * im[j];

        psd[j] = power / (fs * window_power);

        if (j != 0 && j != n_fft / 2) {
            psd[j] *= 2.0;
        }
    }
}

/* Hann을 적용하지 않은 신호의 선형 ACF. n_fft >= 2*n-1로 순환 겹침 방지.
 * FFT -> |FFT|^2 -> IFFT -> lag 0으로 나누기. 긴 lag 보정은 하지 않는다. */
static int amp_acf(const float *input, size_t n, size_t stride, size_t k,
                   float *re, float *im, size_t n_fft) {

    memset(re, 0, n_fft * sizeof(*re));
    memset(im, 0, n_fft * sizeof(*im));

    for (size_t g = 0; g < n; g++) {
        re[g] = input[g * stride + k];
    }

    if (csi_fft(re, im, n_fft) != 0) {
        return 1;
    }

    for (size_t j = 0; j < n_fft; j++) {
        double power = (double)re[j] * re[j] + (double)im[j] * im[j];
        if (!isfinite(power) || power > FLT_MAX) {
            return 1;
        }
        re[j] = (float)power;
        im[j] = 0;
    }

    if (csi_ifft(re, im, n_fft) != 0 || !isfinite(re[0]) || re[0] <= 0) {
        return 1;
    }

    double energy = re[0];

    for (size_t j = 0; j < n; j++) {
        re[j] = (float)(re[j] / energy);
    }

    return 0;
}

static int amp_measure_psd(const double *psd, size_t n_fft,
                           const csi_amp_analysis_config_t *cfg,
                           const csi_amp_result_t *result,
                           csi_amp_bin_result_t *bin, int *band_edge) {
    double fs = result->sample_rate_hz;
    double df = fs / n_fft;
    size_t first = (size_t)ceil(cfg->min_hz / df);
    size_t last = (size_t)floor(cfg->max_hz / df);

    if (first > last || last > n_fft / 2) {
        bin->reasons |= CSI_AMP_SHORT;
        return 1;
    }

    size_t peak = first;

    for (size_t j = first + 1; j <= last; j++) {
        if (psd[j] > psd[peak]) {
            peak = j;
        }
    }

    bin->has_psd = 1;
    bin->psd_hz = peak * df;
    bin->psd_bpm = 60.0 * bin->psd_hz;
    bin->psd_peak = psd[peak];

    double total = 0, neighborhood = 0;

    for (size_t j = 1; j <= n_fft / 2; j++) {
        total += psd[j];
        if (fabs(j * df - bin->psd_hz) <= result->resolution_hz) {
            neighborhood += psd[j];
        }
    }
    bin->concentration = total > 0 ? neighborhood / total : 0;
    bin->cycles = bin->psd_hz * result->duration_s;

    *band_edge = peak == first || peak == last;
    return 0;
}

static void amp_measure_acf(const float *acf, size_t n,
                            const csi_amp_analysis_config_t *cfg,
                            const csi_amp_result_t *result,
                            csi_amp_bin_result_t *bin) {
    double fs = result->sample_rate_hz;

    /* find_peaks처럼 plateau는 가운데(짝수 폭이면 왼쪽)를 고른다. */
    size_t best = 0;
    double min_lag = 1.0 / cfg->max_hz;
    double max_lag = fmin(1.0 / cfg->min_hz, (n - 1) / (2.0 * fs));

    for (size_t i = 1; i + 1 < n;) {
        if (acf[i] > acf[i - 1]) {
            size_t right = i;
            while (right + 1 < n && acf[right + 1] == acf[i]) {
                right++;
            }
            size_t middle = i + (right - i) / 2;
            double lag = middle / fs;
            if (right + 1 < n && acf[right] > acf[right + 1] &&
                lag >= min_lag && lag <= max_lag && acf[middle] > 0 &&
                (best == 0 || acf[middle] > acf[best])) {
                best = middle;
            }
            i = right + 1;
        } else {
            i++;
        }
    }

    if (best != 0) {
        double left = acf[best - 1], center = acf[best], right = acf[best + 1];
        double curvature = left - 2 * center + right;
        double offset = curvature < 0 ? 0.5 * (left - right) / curvature : 0;
        offset = fmax(-0.5, fmin(0.5, offset));
        bin->has_acf = 1;
        bin->acf_lag_s = (best + offset) / fs;
        bin->acf_hz = 1.0 / bin->acf_lag_s;
        bin->acf_bpm = 60.0 * bin->acf_hz;
        bin->acf_peak = acf[best];
    }
}

static void amp_score(const double *psd, size_t n_fft, const float *acf,
                      size_t n, const csi_amp_analysis_config_t *cfg,
                      const csi_amp_result_t *result,
                      csi_amp_bin_result_t *bin) {

    int band_edge;

    if (amp_measure_psd(psd, n_fft, cfg, result, bin, &band_edge)) {
        return;
    }

    amp_measure_acf(acf, n, cfg, result, bin);

    if (result->duration_s < cfg->min_duration_s) {
        bin->reasons |= CSI_AMP_SHORT;
    }
    if (band_edge) {
        bin->reasons |= CSI_AMP_BAND_EDGE;
    }
    if (bin->cycles < cfg->min_cycles) {
        bin->reasons |= CSI_AMP_FEW_CYCLES;
    }
    if (bin->concentration < cfg->min_concentration) {
        bin->reasons |= CSI_AMP_DIFFUSE;
    }
    if (!bin->has_acf || bin->acf_peak < cfg->min_acf) {
        bin->reasons |= CSI_AMP_WEAK_ACF;
    }
    if (!bin->has_acf ||
        fabs(bin->psd_hz - bin->acf_hz) >
            cfg->agreement_resolution_bins * result->resolution_hz) {
        bin->reasons |= CSI_AMP_DISAGREEMENT;
    }
    bin->score = bin->concentration * fmax(0, bin->acf_peak);
    bin->accepted = bin->reasons == 0;
}

static size_t amp_grid_capacity(const csi_frame_t *frames, size_t n_frames,
                                const csi_amp_context_t *context) {
    if (frames == NULL || n_frames < 2 || context == NULL ||
        context->coeffs == NULL || context->filter_info.n_taps == 0 ||
        context->filter_info.down == 0 || frames[0].n == 0 ||
        frames[0].n > CSI_MAX_SUBCARRIERS) {
        return 0;
    }

    if (!amp_analysis_valid(&context->analysis,
                            context->filter_info.output_fs)) {
        return 0;
    }

    size_t n_bins = frames[0].n;

    for (size_t i = 1; i < n_frames; i++) {
        if (frames[i].n != n_bins ||
            rel_time(frames, i) < rel_time(frames, i - 1)) {
            return 0;
        }
    }

    size_t max_grid =
        (size_t)(rel_time(frames, n_frames - 1) / CSI_INTERP_STEP_US) + 1;

    size_t n_taps = context->filter_info.n_taps;

    if (max_grid < n_taps || max_grid > SIZE_MAX / sizeof(float) / n_bins) {
        return 0;
    }

    return max_grid;
}

static int amp_reduce_grid(float *filtered, size_t n_filtered, size_t n_bins,
                           size_t n_taps, uint32_t start_us,
                           const csi_amp_context_t *context,
                           csi_amp_result_t *result) {
    uint32_t reduced_start_us;
    uint64_t reduced_step_us;

    size_t n_reduced =
        amp_downsample(filtered, n_filtered, n_bins, (n_taps - 1) / 2,
                       context->filter_info.down, start_us, &reduced_start_us,
                       &reduced_step_us);

    if (n_reduced < 2) {
        return 1;
    }

    result->n_bins = n_bins;
    result->n_samples = n_reduced;
    result->start_us = reduced_start_us;
    result->step_us = reduced_step_us;
    result->sample_rate_hz = context->filter_info.output_fs;
    result->duration_s = (n_reduced - 1) / result->sample_rate_hz;
    result->resolution_hz = result->sample_rate_hz / n_reduced;

    return 0;
}

static int amp_resample(const csi_frame_t *frames, size_t n_frames,
                        const csi_amp_context_t *context,
                        csi_amp_result_t *result, float **samples,
                        uint8_t *eligible) {
    size_t max_grid = amp_grid_capacity(frames, n_frames, context);

    if (max_grid == 0) {
        return 1;
    }

    size_t n_bins = frames[0].n, n_taps = context->filter_info.n_taps;

    float *out = malloc(max_grid * n_bins * sizeof(*out));
    float *filtered = NULL;
    int status = 1;
    uint32_t start_us;

    if (out == NULL) {
        goto cleanup;
    }

    size_t n_grid =
        csi_amp_interp(frames, n_frames, out, eligible, max_grid, &start_us);

    if (n_grid == 0 || n_grid < n_taps) {
        goto cleanup;
    }

    size_t n_filtered = n_grid - n_taps + 1;
    filtered = malloc(n_filtered * n_bins * sizeof(*filtered));

    if (filtered == NULL) {
        goto cleanup;
    }

    if (context->filter_info.down == 1) {
        memcpy(filtered, out + ((n_taps - 1) / 2) * n_bins,
               n_filtered * n_bins * sizeof(*filtered));

    } else if (fir_filter_valid(out, n_grid, n_bins, context->coeffs, n_taps,
                                filtered, n_filtered) != n_filtered) {
        goto cleanup;
    }

    free(out);

    // apply downsamplings
    out = NULL;
    if (amp_reduce_grid(filtered, n_filtered, n_bins, n_taps, start_us, context,
                        result)) {
        goto cleanup;
    }

    *samples = filtered;
    filtered = NULL;
    status = 0;

cleanup:
    free(filtered);
    free(out);
    return status;
}

static int amp_detrend_column(float *filtered, size_t n_reduced, size_t n_bins,
                              size_t k, float *hann, double flat_floor,
                              csi_amp_bin_result_t *bin) {
    double mean = 0.0;

    for (size_t g = 0; g < n_reduced; g++) {
        if (!isfinite(filtered[g * n_bins + k])) {
            return 1;
        }
        mean += filtered[g * n_bins + k];
        hann[g] = filtered[g * n_bins + k];
    }

    qsort(hann, n_reduced, sizeof(*hann), amp_float_compare);

    double median =
        n_reduced % 2
            ? hann[n_reduced / 2]
            : ((double)hann[n_reduced / 2 - 1] + hann[n_reduced / 2]) / 2;

    mean /= n_reduced;

    double center = (n_reduced - 1) / 2.0;
    double numerator = 0.0;
    double denominator = 0.0;

    for (size_t g = 0; g < n_reduced; g++) {
        double t = (double)g - center;
        double x = filtered[g * n_bins + k] - mean;

        numerator += t * x;
        denominator += t * t;
    }

    double slope = numerator / denominator;

    for (size_t g = 0; g < n_reduced; g++) {
        double trend = mean + slope * ((double)g - center);
        filtered[g * n_bins + k] = ((float)(filtered[g * n_bins + k] - trend));
    }

    double residual_mean = 0.0;

    for (size_t g = 0; g < n_reduced; g++) {
        residual_mean += filtered[g * n_bins + k];
    }

    residual_mean /= n_reduced;
    double energy = 0;

    for (size_t g = 0; g < n_reduced; g++) {
        filtered[g * n_bins + k] =
            (float)(filtered[g * n_bins + k] - residual_mean);
        double x = filtered[g * n_bins + k];
        energy += x * x;
    }

    if (sqrt(energy / n_reduced) <= flat_floor * fmax(1.0, median)) {
        bin->reasons = CSI_AMP_FLAT;
    }
    return 0;
}

static int amp_detrend(float *samples, float *scratch, const uint8_t *eligible,
                       const csi_amp_context_t *context,
                       csi_amp_result_t *result) {
    for (size_t k = 0; k < result->n_bins; k++) {
        csi_amp_bin_result_t *bin = &result->bins[k];
        bin->eligible = eligible[k];

        if (!eligible[k]) {
            bin->reasons = CSI_AMP_EXCLUDED;
            continue;
        }

        if (amp_detrend_column(samples, result->n_samples, result->n_bins, k,
                               scratch, context->analysis.flat_relative_floor,
                               bin)) {
            return 1;
        }
    }

    return 0;
}

typedef struct {
    size_t n_fft, n_corr_fft;
    float *re, *im;
    double *psd;
} amp_spectrum_workspace;

static void amp_spectrum_free(amp_spectrum_workspace *work) {
    free(work->re);
    free(work->im);
    free(work->psd);
}

static int amp_spectrum_init(amp_spectrum_workspace *work, size_t n_reduced,
                             size_t fft_oversampling) {
    if (n_reduced > SIZE_MAX / fft_oversampling) {
        return 1;
    }

    size_t n_fft = 1;

    if (n_reduced > SIZE_MAX / 2) {
        return 1;
    }

    size_t required = n_reduced * fft_oversampling;

    while (n_fft < required) {
        if (n_fft > SIZE_MAX / 2) {
            return 1;
        }
        n_fft *= 2;
    }

    size_t n_corr_fft = 1;

    while (n_corr_fft < 2 * n_reduced - 1) {
        if (n_corr_fft > SIZE_MAX / 2) {
            return 1;
        }
        n_corr_fft *= 2;
    }

    size_t fft_capacity = n_fft > n_corr_fft ? n_fft : n_corr_fft;

    if (fft_capacity > SIZE_MAX / sizeof(float)) {
        return 1;
    }

    work->re = malloc(fft_capacity * sizeof(*work->re));
    work->im = malloc(fft_capacity * sizeof(*work->im));

    if (n_fft / 2 + 1 > SIZE_MAX / sizeof(*work->psd)) {
        return 1;
    }

    work->psd = malloc((n_fft / 2 + 1) * sizeof(*work->psd));

    if (work->re == NULL || work->im == NULL || work->psd == NULL) {
        return 1;
    }

    work->n_fft = n_fft;
    work->n_corr_fft = n_corr_fft;
    return 0;
}

static int amp_analyze_columns(const float *filtered, float *hann,
                               const csi_amp_context_t *context,
                               csi_amp_result_t *result) {
    amp_spectrum_workspace work = {0};
    size_t n_reduced = result->n_samples, n_bins = result->n_bins;
    int status = 1;

    if (amp_spectrum_init(&work, n_reduced,
                          context->analysis.fft_oversampling)) {
        goto cleanup;
    }

    size_t n_fft = work.n_fft, n_corr_fft = work.n_corr_fft;
    float *fft_re = work.re, *fft_im = work.im;
    double *psd = work.psd;

    result->n_fft = n_fft;

    amp_hann(hann, n_reduced);

    double window_power = 0;

    for (size_t g = 0; g < n_reduced; g++) {
        window_power += (double)hann[g] * hann[g];
    }

    for (size_t k = 0; k < n_bins; k++) {
        csi_amp_bin_result_t *bin = &result->bins[k];
        if (!bin->eligible || (bin->reasons & CSI_AMP_FLAT)) {
            continue;
        }
        memset(fft_re, 0, n_fft * sizeof(*fft_re));
        memset(fft_im, 0, n_fft * sizeof(*fft_im));

        for (size_t g = 0; g < n_reduced; g++) {
            fft_re[g] = filtered[g * n_bins + k] * hann[g];
        }

        if (csi_fft(fft_re, fft_im, n_fft) != 0) {
            goto cleanup;
        }

        amp_psd(fft_re, fft_im, n_fft, result->sample_rate_hz, window_power,
                psd);

        if (amp_acf(filtered, n_reduced, n_bins, k, fft_re, fft_im,
                    n_corr_fft) != 0) {
            goto cleanup;
        }

        amp_score(psd, n_fft, fft_re, n_reduced, &context->analysis, result,
                  bin);

        if (bin->has_psd &&
            (result->best_bin < 0 ||
             bin->score > result->bins[result->best_bin].score)) {
            result->best_bin = (int)k;
        }

        if (bin->accepted &&
            (result->selected_bin < 0 ||
             bin->score > result->bins[result->selected_bin].score)) {
            result->selected_bin = (int)k;
        }
    }

    status = 0;

cleanup:
    amp_spectrum_free(&work);
    return status;
}

// 공용 진입점: 보간/재표본화 -> 추세 제거 -> PSD/ACF와 후보 판정.
int csi_amp_process(const csi_frame_t *frames, size_t n_frames,
                    const csi_amp_context_t *context,
                    csi_amp_result_t *result) {
    if (result == NULL) {
        return 1;
    }

    memset(result, 0, sizeof(*result));
    result->best_bin = result->selected_bin = -1;

    float *samples = NULL, *scratch = NULL;
    uint8_t eligible[CSI_MAX_SUBCARRIERS];
    int status = 1;

    if (amp_resample(frames, n_frames, context, result, &samples, eligible)) {
        goto cleanup;
    }

    scratch = malloc(result->n_samples * sizeof(*scratch));

    if (!scratch || amp_detrend(samples, scratch, eligible, context, result)) {
        goto cleanup;
    }

    status = amp_analyze_columns(samples, scratch, context, result);

cleanup:
    free(scratch);
    free(samples);
    return status;
}
