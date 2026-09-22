/* Reference tests only: expose internal stages without widening the public API. */
#include "../src/amplitude.c"

int test_spectra(const float *x, size_t n, double fs, size_t n_fft,
                 double *psd, float *acf) {
    float *w = malloc(n * sizeof(*w));
    float *re = calloc(n_fft, sizeof(*re));
    float *im = calloc(n_fft, sizeof(*im));
    int status = 1;
    if (!w || !re || !im || n_fft < 2 * n - 1) goto done;
    amp_hann(w, n);
    double power = 0;
    for (size_t i = 0; i < n; i++) {
        re[i] = x[i] * w[i];
        power += (double)w[i] * w[i];
    }
    if (csi_fft(re, im, n_fft)) goto done;
    amp_psd(re, im, n_fft, fs, power, psd);
    if (amp_acf(x, n, 1, 0, re, im, n_fft)) goto done;
    memcpy(acf, re, n * sizeof(*acf));
    status = 0;
done:
    free(w); free(re); free(im);
    return status;
}

int test_pipeline(const float *values, const uint8_t *valid, size_t rows,
                  size_t bins, uint32_t step, float half_width,
                  csi_amp_result_t *result) {
    if (bins > CSI_MAX_SUBCARRIERS) return 1;
    csi_frame_t *frames = calloc(rows, sizeof(*frames));
    csi_amp_context_t ctx = {0};
    firwin_config_t cfg = {.target_fs=10, .half_width_s=half_width, .beta=8};
    int status = 1;
    if (!frames || csi_amp_init(&ctx, &cfg)) goto done;
    for (size_t i=0;i<rows;i++) {
        frames[i].n = (uint16_t)bins;
        frames[i].meta.timestamp = 4294000000u + (uint32_t)(i * step);
        for (size_t k=0;k<bins;k++) {
            frames[i].re[k] = values[i*bins+k];
            frames[i].valid[k] = valid[i*bins+k];
        }
    }
    status = csi_amp_process(frames, rows, &ctx, result);
done:
    csi_amp_free(&ctx); free(frames);
    return status;
}

void test_score(const double *psd, size_t n_fft, const float *acf,
                size_t n, double fs, csi_amp_bin_result_t *bin) {
    csi_amp_analysis_config_t config = csi_amp_default_analysis_config();
    csi_amp_result_t result = {.sample_rate_hz=fs, .duration_s=(n-1)/fs,
                               .resolution_hz=fs/n};
    memset(bin, 0, sizeof(*bin));
    bin->eligible = 1;
    amp_score(psd, n_fft, acf, n, &config, &result, bin);
}
