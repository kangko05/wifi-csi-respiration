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
static int amp_fft_dispatch(float *re, float *im, size_t n,
                             const csi_fft_plan_t *plan, int float_math,
                             const csi_fft_q15_plan_t *q15_plan,
                             int16_t *q15_work, int inverse) {
    if (q15_plan)
        return csi_fft_q15_float(re, im, n, q15_plan, q15_work, 2*n, inverse);
    if (float_math) return csi_fft_float_planned(re, im, n, plan, inverse);
    if (plan) return inverse ? csi_ifft_planned(re, im, n, plan) :
                               csi_fft_planned(re, im, n, plan);
    return inverse ? csi_ifft(re, im, n) : csi_fft(re, im, n);
}

static int amp_acf_planned(const float *input, size_t n, size_t stride, size_t k,
                           float *re, float *im, size_t n_fft,
                           const csi_fft_plan_t *plan, int float_math,
                           const csi_fft_q15_plan_t *q15_plan, int16_t *q15_work) {

    memset(re, 0, n_fft * sizeof(*re));
    memset(im, 0, n_fft * sizeof(*im));

    for (size_t g = 0; g < n; g++) {
        re[g] = input[g * stride + k];
    }

    if (amp_fft_dispatch(re, im, n_fft, plan, float_math, q15_plan, q15_work, 0) != 0) {
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

    if (amp_fft_dispatch(re, im, n_fft, plan, float_math, q15_plan, q15_work, 1) != 0 ||
        !isfinite(re[0]) || re[0] <= 0) {
        return 1;
    }

    double energy = re[0];

    for (size_t j = 0; j < n; j++) {
        re[j] = (float)(re[j] / energy);
    }

    return 0;
}

static int amp_acf(const float *input, size_t n, size_t stride, size_t k,
                   float *re, float *im, size_t n_fft) {
    return amp_acf_planned(input, n, stride, k, re, im, n_fft, NULL, 0, NULL, NULL);
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

static int amp_raw_valid_bin(const csi_record_t *record, size_t k) {
    return !(record->first_word_invalid && record->len >= 4 && k < 2);
}

static size_t amp_raw_next(const csi_amp_raw_source_t *source, size_t n,
                            size_t k, size_t i) {
    while (i < n && !amp_raw_valid_bin(source->read(source->user, i), k)) {
        ++i;
    }
    return i;
}

static float amp_raw_value(const csi_record_t *record, size_t k) {
    return csi_abs(record->buf[2 * k + 1], record->buf[2 * k]);
}

static void amp_raw_trace(const csi_amp_raw_source_t *source,
                           csi_amp_stage_t stage, size_t bin, size_t bins) {
    if (source->trace) source->trace(source->trace_user, stage, bin, bins);
}

enum { AMP_FIXED_SIDE = 129, AMP_COEFF_SHIFT = 23 };

static int32_t amp_fixed_value(const csi_record_t *r, size_t k,
                                const uint32_t *magnitudes) {
    unsigned re = (unsigned)abs((int)r->buf[2*k+1]);
    unsigned im = (unsigned)abs((int)r->buf[2*k]);
    return (int32_t)magnitudes[re*AMP_FIXED_SIDE + im];
}

static int32_t amp_round_q15_product(int64_t v) {
    return (int32_t)(v >= 0 ? (v + 16384) / 32768 : -((-v + 16384) / 32768));
}

int csi_amp_process_raw(const csi_amp_raw_source_t *source, size_t n_records,
                        const csi_amp_context_t *context,
                        csi_amp_result_t *result) {
    if (!result) {
        return 1;
    }
    memset(result, 0, sizeof(*result));
    result->best_bin = result->selected_bin = -1;
    if (!source || !source->read || n_records < 2 || !context ||
        !context->coeffs || !context->filter_info.n_taps ||
        !context->filter_info.down ||
        (source->prepared && source->fixed_preprocess) ||
        !amp_analysis_valid(&context->analysis, context->filter_info.output_fs)) {
        return 1;
    }
    amp_raw_trace(source, CSI_AMP_STAGE_VALIDATE, 0, 0);
    const csi_record_t *first_record = source->read(source->user, 0);
    if (!first_record || !first_record->len || first_record->len > CSI_BUF_MAX ||
        first_record->len % 2) {
        return 1;
    }
    uint16_t len = first_record->len;
    uint32_t origin = first_record->timestamp, end = 0;
    size_t n_bins = len / 2;
    for (size_t i = 0; i < n_records; ++i) {
        const csi_record_t *record = source->read(source->user, i);
        if (!record || record->len != len || !isfinite(record->compensate_gain) ||
            (uint32_t)(record->timestamp - origin) < end) {
            return 1;
        }
        end = record->timestamp - origin;
    }

    /* Use the intersection of all eligible columns' support, as in the
     * original multicolumn interpolation. Independent per-bin starts differ. */
    uint8_t eligible[CSI_MAX_SUBCARRIERS] = {0};
    uint32_t start = 0, stop = end;
    size_t n_eligible = 0;
    for (size_t k = 0; k < n_bins; ++k) {
        amp_raw_trace(source, CSI_AMP_STAGE_YIELD, k, n_bins);
        if (source->yield) source->yield(source->yield_user);
        amp_raw_trace(source, CSI_AMP_STAGE_ELIGIBILITY, k, n_bins);
        size_t count = 0;
        uint32_t first = 0, prev = 0, max_gap = 0;
        for (size_t i = 0; i < n_records; ++i) {
            const csi_record_t *record = source->read(source->user, i);
            if (!amp_raw_valid_bin(record, k)) {
                continue;
            }
            uint32_t t = record->timestamp - origin;
            if (count == 0) first = t;
            if (t - prev > max_gap) max_gap = t - prev;
            prev = t;
            ++count;
        }
        if (end - prev > max_gap) max_gap = end - prev;
        eligible[k] = count >= CSI_INTERP_MIN_PACKETS &&
                       (float)count / (float)n_records >= CSI_INTERP_MIN_FRACTION &&
                       max_gap <= CSI_INTERP_MAX_GAP_US;
        if (eligible[k]) {
            ++n_eligible;
            if (first > start) start = first;
            if (prev < stop) stop = prev;
        }
    }
    amp_raw_trace(source, CSI_AMP_STAGE_SETUP, 0, n_bins);
    if (!n_eligible || stop < start) return 1;
    size_t n_grid = (stop - start) / CSI_INTERP_STEP_US + 1;
    size_t taps = context->filter_info.n_taps;
    size_t down = context->filter_info.down;
    if (n_grid < taps || n_grid > SIZE_MAX / sizeof(float)) return 1;
    size_t half = (taps - 1) / 2;
    size_t offset = (down - half % down) % down;
    size_t n_filtered = n_grid - taps + 1;
    if (offset >= n_filtered) return 1;
    size_t n_reduced = (n_filtered - 1 - offset) / down + 1;
    if (n_reduced < 2) return 1;
#if SIZE_MAX > UINT64_MAX / CSI_INTERP_STEP_US
    if (down > UINT64_MAX / CSI_INTERP_STEP_US) return 1;
#endif
    result->n_bins = n_bins;
    result->n_samples = n_reduced;
    result->start_us = start + (uint32_t)((half + offset) * CSI_INTERP_STEP_US);
    result->step_us = (uint64_t)down * CSI_INTERP_STEP_US;
    result->sample_rate_hz = context->filter_info.output_fs;
    result->duration_s = (n_reduced - 1) / result->sample_rate_hz;
    result->resolution_hz = result->sample_rate_hz / n_reduced;

    if (source->prepared && source->prepared_rows != n_reduced) return 1;
    uint8_t selected[CSI_MAX_SUBCARRIERS];
    memcpy(selected, eligible, sizeof(selected));
    if (source->max_bins && source->max_bins < n_eligible) {
        double quality[CSI_MAX_SUBCARRIERS] = {0};
        memset(selected, 0, sizeof(selected));
        for (size_t k = 0; k < n_bins; ++k) {
            if (!eligible[k]) continue;
            double sum = 0, sq = 0;
            size_t count = 0;
            for (size_t i = 0; i < n_records; i += 10) {
                const csi_record_t *r = source->read(source->user, i);
                if (!amp_raw_valid_bin(r, k)) continue;
                double a = amp_raw_value(r, k);
                sum += a; sq += a * a; ++count;
            }
            quality[k] = count && sum > 0 ? fmax(0, sq * count / (sum * sum) - 1) : 0;
        }
        for (size_t j = 0; j < source->max_bins; ++j) {
            size_t best = n_bins;
            for (size_t k = 0; k < n_bins; ++k)
                if (eligible[k] && !selected[k] &&
                    (best == n_bins || quality[k] > quality[best])) best = k;
            if (best < n_bins) selected[best] = 1;
        }
    }
    float *grid = source->fixed_preprocess ? NULL : malloc(n_grid * sizeof(*grid));
    int32_t *fixed_grid = source->fixed_preprocess ? malloc(n_grid * sizeof(*fixed_grid)) : NULL;
    uint32_t *magnitudes = NULL;
    int32_t *fixed_coeffs = NULL;
    float *reduced = malloc(n_reduced * sizeof(*reduced));
    float *scratch = malloc(n_reduced * sizeof(*scratch));
    amp_spectrum_workspace work = {0};
    int16_t *q15_work = NULL;
    int status = 1;
    if ((!source->fixed_preprocess && !grid) ||
        (source->fixed_preprocess && !fixed_grid) || !reduced || !scratch ||
        amp_spectrum_init(&work, n_reduced, context->analysis.fft_oversampling)) {
        goto cleanup_raw;
    }
    if (source->fixed_preprocess) {
        if (taps > SIZE_MAX / sizeof(*fixed_coeffs)) goto cleanup_raw;
        magnitudes = malloc(AMP_FIXED_SIDE * AMP_FIXED_SIDE * sizeof(*magnitudes));
        fixed_coeffs = malloc(taps * sizeof(*fixed_coeffs));
        if (!magnitudes || !fixed_coeffs) goto cleanup_raw;
        for (unsigned a=0; a<AMP_FIXED_SIDE; ++a)
            for (unsigned b=0; b<AMP_FIXED_SIDE; ++b)
                magnitudes[a*AMP_FIXED_SIDE+b] = csi_abs_q12((int8_t)(-(int)a), (int8_t)(-(int)b));
        uint64_t coefficient_sum = 0;
        for (size_t j=0; j<taps; ++j) {
            double scaled = (double)context->coeffs[j] * 8388608.0;
            if (!isfinite(scaled) || scaled < INT32_MIN || scaled > INT32_MAX) goto cleanup_raw;
            fixed_coeffs[j] = (int32_t)llround(scaled);
            uint64_t magnitude = fixed_coeffs[j] < 0 ?
                (uint64_t)(-(int64_t)fixed_coeffs[j]) : (uint64_t)fixed_coeffs[j];
            /* Q12 amplitudes/interpolants <= sqrt(32768)*4096 < 741456.
             * Reject coefficients that could overflow any int64 partial sum. */
            if (magnitude > (uint64_t)INT64_MAX/741456 - coefficient_sum) goto cleanup_raw;
            coefficient_sum += magnitude;
        }
    }
    if (source->q15_plan) {
        size_t capacity = work.n_fft > work.n_corr_fft ? work.n_fft : work.n_corr_fft;
        if (capacity > SIZE_MAX / (2*sizeof(*q15_work))) goto cleanup_raw;
        q15_work = malloc(2*capacity*sizeof(*q15_work));
        if (!q15_work) goto cleanup_raw;
    }
    result->n_fft = work.n_fft;
    for (size_t k = 0; k < n_bins; ++k) {
        amp_raw_trace(source, CSI_AMP_STAGE_YIELD, k, n_bins);
        if (source->yield) source->yield(source->yield_user);
        csi_amp_bin_result_t *bin = &result->bins[k];
        bin->eligible = eligible[k];
        if (!eligible[k]) {
            bin->reasons = CSI_AMP_EXCLUDED;
            continue;
        }
        if (!selected[k]) {
            bin->reasons = CSI_AMP_NOT_SELECTED;
            continue;
        }
        if (source->prepared) {
            for (size_t g = 0; g < n_reduced; ++g)
                reduced[g] = source->prepared[g * n_bins + k];
        } else {
        amp_raw_trace(source, CSI_AMP_STAGE_INTERPOLATE, k, n_bins);
        size_t lo = amp_raw_next(source, n_records, k, 0);
        size_t hi = amp_raw_next(source, n_records, k, lo + 1);
        int32_t fixed_low = 0, fixed_high = 0;
        if (source->fixed_preprocess) {
            fixed_low = amp_fixed_value(source->read(source->user, lo), k, magnitudes);
            if (hi < n_records) fixed_high = amp_fixed_value(source->read(source->user, hi), k, magnitudes);
        }
        for (size_t g = 0; g < n_grid; ++g) {
            uint32_t x = start + (uint32_t)(g * CSI_INTERP_STEP_US);
            while (hi < n_records &&
                   (uint32_t)(source->read(source->user, hi)->timestamp - origin) <= x) {
                lo = hi;
                hi = amp_raw_next(source, n_records, k, hi + 1);
                if (source->fixed_preprocess) {
                    fixed_low = fixed_high;
                    if (hi < n_records)
                        fixed_high = amp_fixed_value(source->read(source->user, hi), k, magnitudes);
                }
            }
            const csi_record_t *low = source->read(source->user, lo);
            uint32_t t_lo = low->timestamp - origin;
            if (source->fixed_preprocess) {
                fixed_grid[g] = fixed_low;
                if (hi < n_records && t_lo != x) {
                    uint32_t dt = source->read(source->user, hi)->timestamp - origin - t_lo;
                    if (!dt || dt > CSI_INTERP_MAX_GAP_US) goto cleanup_raw;
                    /* 100000 us * 32768 fits uint32; no soft 64-bit division. */
                    uint32_t weight = ((x-t_lo)*UINT32_C(32768) + dt/2) / dt;
                    fixed_grid[g] += amp_round_q15_product((int64_t)(fixed_high-fixed_low)*weight);
                }
                continue;
            }
            float a_lo = amp_raw_value(low, k);
            if (hi >= n_records || t_lo == x) {
                grid[g] = a_lo;
            } else {
                const csi_record_t *high = source->read(source->user, hi);
                uint32_t t_hi = high->timestamp - origin;
                float frac = (float)(x - t_lo) / (float)(t_hi - t_lo);
                grid[g] = a_lo + frac * (amp_raw_value(high, k) - a_lo);
            }
        }
        amp_raw_trace(source, CSI_AMP_STAGE_FIR, k, n_bins);
        for (size_t g = 0; g < n_reduced; ++g) {
            size_t pos = offset + g * down;
            if (source->fixed_preprocess) {
                if (down == 1) reduced[g] = fixed_grid[pos+half] * 0x1p-12f;
                else {
                    int64_t sum = 0;
                    for (size_t j=0; j<taps; ++j)
                        sum += (int64_t)fixed_coeffs[taps-1-j] * fixed_grid[pos+j];
                    reduced[g] = (float)sum * 0x1p-35f;
                }
            } else if (down == 1) {
                reduced[g] = grid[pos + half];
            } else if (source->float_math) {
                float sum = 0;
                for (size_t j = 0; j < taps; ++j)
                    sum += context->coeffs[taps - 1 - j] * grid[pos + j];
                reduced[g] = sum;
            } else {
                /* Same coefficient order, double accumulation and float cast
                 * as fir_filter_valid, skipping outputs discarded by decimation. */
                double sum = 0;
                for (size_t j = 0; j < taps; ++j) {
                    sum += (double)context->coeffs[taps - 1 - j] * grid[pos + j];
                }
                reduced[g] = (float)sum;
            }
        }
        } /* prepared or batch preprocessing */
        if (source->observe_reduced)
            source->observe_reduced(source->observe_user, k, reduced, n_reduced);
        amp_raw_trace(source, CSI_AMP_STAGE_DETREND, k, n_bins);
        if (amp_detrend_column(reduced, n_reduced, 1, 0, scratch,
                               context->analysis.flat_relative_floor, bin)) {
            goto cleanup_raw;
        }
        if (bin->reasons & CSI_AMP_FLAT) continue;
        amp_raw_trace(source, CSI_AMP_STAGE_PSD, k, n_bins);
        amp_hann(scratch, n_reduced);
        double window_power = 0;
        memset(work.re, 0, work.n_fft * sizeof(*work.re));
        memset(work.im, 0, work.n_fft * sizeof(*work.im));
        for (size_t g = 0; g < n_reduced; ++g) {
            window_power += (double)scratch[g] * scratch[g];
            work.re[g] = reduced[g] * scratch[g];
        }
        if (amp_fft_dispatch(work.re, work.im, work.n_fft, source->fft_plan,
                             source->float_math, source->q15_plan, q15_work, 0))
            goto cleanup_raw;
        amp_psd(work.re, work.im, work.n_fft, result->sample_rate_hz,
                window_power, work.psd);
        amp_raw_trace(source, CSI_AMP_STAGE_ACF, k, n_bins);
        if (amp_acf_planned(reduced, n_reduced, 1, 0, work.re, work.im,
                             work.n_corr_fft, source->fft_plan, source->float_math,
                             source->q15_plan, q15_work)) {
            goto cleanup_raw;
        }
        amp_raw_trace(source, CSI_AMP_STAGE_SCORE, k, n_bins);
        amp_score(work.psd, work.n_fft, work.re, n_reduced,
                  &context->analysis, result, bin);
        if (bin->has_psd && (result->best_bin < 0 ||
            bin->score > result->bins[result->best_bin].score)) {
            result->best_bin = (int)k;
        }
        if (bin->accepted && (result->selected_bin < 0 ||
            bin->score > result->bins[result->selected_bin].score)) {
            result->selected_bin = (int)k;
        }
    }
    status = 0;
cleanup_raw:
    free(fixed_coeffs);
    free(magnitudes);
    free(fixed_grid);
    free(q15_work);
    amp_raw_trace(source, CSI_AMP_STAGE_CLEANUP, 0, n_bins);
    amp_spectrum_free(&work);
    free(scratch);
    free(reduced);
    free(grid);
    return status;
}

struct csi_amp_stream {
    const csi_amp_context_t *ctx;
    float *ring, *data;
    float previous[CSI_MAX_SUBCARRIERS];
    size_t bins, capacity, rows, grids;
    uint32_t origin, previous_time;
    int started, finished, failed, float_math;
};

csi_amp_stream_t *csi_amp_stream_create(const csi_amp_context_t *ctx,
                                        size_t bins, size_t capacity, int float_math) {
    if (!ctx || !ctx->coeffs || !ctx->filter_info.n_taps ||
        !ctx->filter_info.down || !bins || bins > CSI_MAX_SUBCARRIERS ||
        !capacity || capacity > SIZE_MAX / bins / sizeof(float) ||
        ctx->filter_info.n_taps > SIZE_MAX / bins / sizeof(float)) return NULL;
    csi_amp_stream_t *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->ctx = ctx; s->bins = bins; s->capacity = capacity;
    s->float_math = float_math;
    s->ring = malloc(ctx->filter_info.n_taps * bins * sizeof(float));
    s->data = malloc(capacity * bins * sizeof(float));
    if (!s->ring || !s->data) { csi_amp_stream_free(s); return NULL; }
    return s;
}

static int amp_stream_grid(csi_amp_stream_t *s, const float *next, uint32_t t) {
    size_t taps = s->ctx->filter_info.n_taps, down = s->ctx->filter_info.down;
    size_t half = (taps - 1) / 2, g = s->grids;
    uint64_t x = (uint64_t)g * CSI_INTERP_STEP_US;
    float frac = t == s->previous_time ? 0 :
        (float)(x - s->previous_time) / (float)(t - s->previous_time);
    for (size_t k = 0; k < s->bins; ++k) {
        float a = s->previous[k];
        s->ring[(g % taps) * s->bins + k] = x == s->previous_time ? a :
            a + frac * (next[k] - a);
    }
    if (g >= taps - 1 && (g - half) % down == 0) {
        if (s->rows == s->capacity) return 1;
        size_t first = (g + 1) % taps;
        for (size_t k = 0; k < s->bins; ++k) {
            double sum = 0;
            float sumf = 0;
            if (down == 1) {
                sum = s->ring[((first + half) % taps) * s->bins + k];
            } else for (size_t j = 0; j < taps; ++j) {
                float v = s->ring[((first + j) % taps) * s->bins + k];
                float c = s->ctx->coeffs[taps - 1 - j];
                if (s->float_math) sumf += c * v;
                else sum += (double)c * v;
            }
            s->data[s->rows * s->bins + k] =
                s->float_math && down != 1 ? sumf : (float)sum;
        }
        ++s->rows;
    }
    ++s->grids;
    return 0;
}

int csi_amp_stream_push(csi_amp_stream_t *s, const csi_record_t *r) {
    if (!s || s->finished || s->failed) return 1;
    if (!r || r->len != s->bins * 2 || r->first_word_invalid ||
        !isfinite(r->compensate_gain)) { s->failed = 1; return 1; }
    uint32_t t = s->started ? r->timestamp - s->origin : 0;
    if (s->started && (t < s->previous_time ||
        t - s->previous_time > CSI_INTERP_MAX_GAP_US)) {
        s->failed = 1; return 1;
    }
    float next[CSI_MAX_SUBCARRIERS];
    for (size_t k = 0; k < s->bins; ++k) next[k] = amp_raw_value(r, k);
    if (!s->started) { s->started = 1; s->origin = r->timestamp; }
    /* Delay a grid endpoint until the next timestamp: duplicate timestamps
     * must use the last record, matching the batch interpolation. */
    while ((uint64_t)s->grids * CSI_INTERP_STEP_US < t) {
        if (amp_stream_grid(s, next, t)) { s->failed = 1; return 1; }
    }
    memcpy(s->previous, next, s->bins * sizeof(float));
    s->previous_time = t;
    return 0;
}

int csi_amp_stream_finish(csi_amp_stream_t *s) {
    if (!s || !s->started || s->finished || s->failed) return 1;
    s->finished = 1;
    if ((uint64_t)s->grids * CSI_INTERP_STEP_US == s->previous_time &&
        amp_stream_grid(s, s->previous, s->previous_time)) {
        s->failed = 1; return 1;
    }
    return 0;
}
const float *csi_amp_stream_data(const csi_amp_stream_t *s) { return s ? s->data : NULL; }
size_t csi_amp_stream_rows(const csi_amp_stream_t *s) { return s ? s->rows : 0; }
size_t csi_amp_stream_bytes(const csi_amp_stream_t *s) {
    return s ? sizeof(*s) + (s->ctx->filter_info.n_taps + s->capacity) *
        s->bins * sizeof(float) : 0;
}
void csi_amp_stream_free(csi_amp_stream_t *s) {
    if (s) { free(s->ring); free(s->data); free(s); }
}
