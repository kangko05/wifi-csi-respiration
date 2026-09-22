#include "csi_output.h"

#include <inttypes.h>

#include "esp_log.h"

static const char *TAG = "csi-output";

void csi_output_fft_ready(size_t capacity, size_t bytes, int64_t setup_us) {
    ESP_LOGI(TAG, "FFT cache ready: max_n=%u bytes=%u setup=%" PRId64 "us",
             (unsigned)capacity, (unsigned)bytes, setup_us);
}

static const char *const AMP_STAGE_NAMES[CSI_AMP_STAGE_COUNT] = {
    "validate", "eligibility", "setup", "interpolate", "fir", "detrend",
    "psd", "acf", "score", "yield", "cleanup"};

void csi_output_amp_progress(const csi_amp_progress_report_t *r) {
    ESP_LOGI(TAG, "amp progress stage=%s bin=%u/%u elapsed=%" PRId64 "ms",
             AMP_STAGE_NAMES[r->stage], (unsigned)(r->bins ? r->bin + 1 : 0),
             (unsigned)r->bins, r->elapsed_us / 1000);
}

void csi_output_amp_profile(const csi_amp_profile_report_t *r) {
    for (size_t i = 0; i < CSI_AMP_STAGE_COUNT; ++i) {
        ESP_LOGI(TAG, "amp timing stage=%s total=%" PRIu64 "us",
                 AMP_STAGE_NAMES[i], r->stage_us[i]);
    }
    ESP_LOGI(TAG, "amp timing wall=%" PRId64 "us (stage totals exclude output)",
             r->elapsed_us);
}

void csi_output_pipeline(const csi_pipeline_report_t *r) {
    const char *state = r->state == CSI_PIPELINE_RUNNING ? "start" :
                        r->state == CSI_PIPELINE_DONE ? "done" : "waiting";
    ESP_LOGI(TAG, "amp %s window=%u/%u span=%" PRIu32 "ms queued=%u "
                  "queue_drops=%" PRIu32 " capacity_drops=%" PRIu32
                  " rejected=%" PRIu32,
             state, (unsigned)r->count, (unsigned)r->capacity, r->span_ms,
             (unsigned)r->queued, r->queue_drops, r->capacity_drops, r->rejected);
}

void csi_output_amplitude(const csi_amplitude_report_t *r) {
    if (r->status) {
        ESP_LOGW(TAG, "amp result=process_error status=%d elapsed=%" PRId64
                      "us queued=%u (input/gap/config/memory)",
                 r->status, r->elapsed_us, (unsigned)r->queued);
    } else if (r->candidate_bin < 0) {
        ESP_LOGI(TAG, "amp result=withheld no_periodic_candidate "
                      "samples=%u bins=%u elapsed=%" PRId64 "us queued=%u",
                 (unsigned)r->n_samples, (unsigned)r->n_bins,
                 r->elapsed_us, (unsigned)r->queued);
    } else {
        ESP_LOGI(TAG, "amp result=%s bin=%d psd_bpm=%.3f acf_bpm=%.3f "
                      "has_acf=%d score=%.4f reasons=0x%" PRIx32
                      " samples=%u bins=%u elapsed=%" PRId64 "us queued=%u",
                 r->accepted ? "candidate" : "withheld", r->candidate_bin,
                 r->candidate.psd_bpm, r->candidate.acf_bpm,
                 r->candidate.has_acf, r->candidate.score, r->candidate.reasons,
                 (unsigned)r->n_samples, (unsigned)r->n_bins,
                 r->elapsed_us, (unsigned)r->queued);
    }
    if (r->elapsed_us >= INT64_C(1000000)) {
        ESP_LOGW(TAG, "amp exceeds 1s budget; inspect queue_drops "
                      "(200 records hold about 2s at 100Hz)");
    }
}

static void output_heap(const char *name, const csi_heap_report_t *r) {
    ESP_LOGI(TAG, "heap %s (bytes): allocated=%u free=%u min_free=%u largest=%u",
             name, (unsigned)r->allocated, (unsigned)r->free,
             (unsigned)r->minimum_free, (unsigned)r->largest);
}

void csi_output_memory(const csi_memory_report_t *r) {
    output_heap("internal", &r->internal);
    output_heap("psram", &r->psram);
}

void csi_output_window_allocation(size_t bytes, bool success) {
    if (success) {
        ESP_LOGI(TAG, "raw window allocated: %u bytes in PSRAM", (unsigned)bytes);
    } else {
        ESP_LOGE(TAG, "raw window needs %u bytes of contiguous PSRAM; "
                      "check PSRAM configuration and free memory", (unsigned)bytes);
    }
}

void csi_output_benchmark_begin(const char *name, size_t records, uint32_t fingerprint) {
    ESP_LOGI(TAG, "BENCH begin name=%s records=%u fingerprint=%08" PRIx32
             " input=frozen_rx_disabled", name, (unsigned)records, fingerprint);
}
void csi_output_benchmark_end(const csi_benchmark_report_t *r) {
    ESP_LOGI(TAG, "BENCH end name=%s status=%d equal_baseline=%d preprocess_us=%" PRId64
             " max_push_us=%" PRId64 " stream_bytes=%u total_us=%" PRId64,
             r->name, r->status, r->equal_baseline, r->preprocess_us,
             r->max_push_us, (unsigned)r->stream_bytes, r->total_us);
}
void csi_output_benchmark_complete(void) {
    ESP_LOGI(TAG, "BENCH COMPLETE; CSI reception remains disabled until reboot");
}

void csi_output_amp_config(bool float_math, size_t oversampling) {
    ESP_LOGI(TAG, "amp config float_fft_fir=%d fft_oversampling=%u",
             (int)float_math, (unsigned)oversampling);
}

void csi_output_fft_kernel(const csi_fft_kernel_report_t *r) {
    ESP_LOGI(TAG, "FFT KERNEL mode=%s n=%u inverse=%d repeats=%u status=%d total_us=%" PRId64,
             r->mode, (unsigned)r->n, (int)r->inverse, (unsigned)r->repeats,
             r->status, r->elapsed_us);
}
