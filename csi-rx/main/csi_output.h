#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "csi_resp/amplitude.h"

typedef enum {
    CSI_PIPELINE_WAITING,
    CSI_PIPELINE_RUNNING,
    CSI_PIPELINE_DONE
} csi_pipeline_state_t;

typedef struct {
    csi_pipeline_state_t state;
    size_t count, capacity, queued;
    uint32_t span_ms, queue_drops, capacity_drops, rejected;
} csi_pipeline_report_t;

typedef struct {
    int status;                 /* 0: analysis succeeded, even if withheld */
    int candidate_bin;          /* -1: no diagnostic candidate */
    bool accepted;              /* periodic candidate, not confirmed respiration */
    csi_amp_bin_result_t candidate;
    size_t n_samples, n_bins, queued;
    int64_t elapsed_us;
} csi_amplitude_report_t;

typedef struct {
    size_t allocated, free, minimum_free, largest;
} csi_heap_report_t;

typedef struct {
    csi_heap_report_t internal, psram;
} csi_memory_report_t;

typedef struct {
    csi_amp_stage_t stage;
    size_t bin, bins;
    int64_t elapsed_us;
} csi_amp_progress_report_t;

typedef struct {
    uint64_t stage_us[CSI_AMP_STAGE_COUNT];
    int64_t elapsed_us;
} csi_amp_profile_report_t;

/* Synchronous output boundary. Reports are borrowed only during the call.
 * A future asynchronous transport must copy the report into its own queue.
 * Processing/sampling policy must not be implemented by an output backend. */
void csi_output_pipeline(const csi_pipeline_report_t *report);
void csi_output_amplitude(const csi_amplitude_report_t *report);
void csi_output_memory(const csi_memory_report_t *report);
void csi_output_window_allocation(size_t bytes, bool success);
void csi_output_amp_progress(const csi_amp_progress_report_t *report);
void csi_output_amp_profile(const csi_amp_profile_report_t *report);
void csi_output_fft_ready(size_t capacity, size_t bytes, int64_t setup_us);

typedef struct {
    const char *name;
    int status, equal_baseline;
    int64_t preprocess_us, max_push_us, total_us;
    size_t stream_bytes;
} csi_benchmark_report_t;
void csi_output_benchmark_begin(const char *name, size_t records, uint32_t fingerprint);
void csi_output_benchmark_end(const csi_benchmark_report_t *report);
void csi_output_benchmark_complete(void);

void csi_output_amp_config(bool float_math, size_t oversampling);

typedef struct {
    const char *mode;
    size_t n, repeats;
    bool inverse;
    int status;
    int64_t elapsed_us;
} csi_fft_kernel_report_t;
void csi_output_fft_kernel(const csi_fft_kernel_report_t *report);
