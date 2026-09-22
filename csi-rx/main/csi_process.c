#include "csi_process.h"

#include <inttypes.h>
#include <string.h>
#include "esp_wifi.h"

#include "constants_rx.h"
#include "csi_resp/amplitude.h"
#include "csi_window.h"
#include "csi_output.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/task.h"

static csi_window_t s_window;
static TaskHandle_t s_task;
static csi_amp_context_t s_amp;
static csi_amp_result_t *s_amp_result;
static csi_fft_plan_t s_fft_plan;
static double *s_fft_twiddles;
#ifdef CONFIG_CSI_RX_AMP_Q15_BENCHMARK
static csi_fft_q15_plan_t s_q15_plan;
#endif

static const csi_record_t *read_window_record(const void *user, size_t index) {
    return csi_window_at(user, index);
}

static void amplitude_yield(void *user) {
    int64_t *last_yield = user;
    if (esp_timer_get_time() - *last_yield >= INT64_C(50000)) {
        /* Let the idle task run, without modifying the analysis input. */
        vTaskDelay(1);
        *last_yield = esp_timer_get_time();
    }
}

typedef struct {
    csi_amp_profile_report_t report;
    csi_amp_stage_t current;
    bool active;
    uint32_t seen;
    int64_t started, stage_started, last_progress;
} amplitude_profiler_t;

static void amplitude_trace(void *user, csi_amp_stage_t stage,
                             size_t bin, size_t bins) {
    amplitude_profiler_t *p = user;
    int64_t now = esp_timer_get_time();
    if (p->active) {
        p->report.stage_us[p->current] += (uint64_t)(now - p->stage_started);
    }
    /* Show each phase at least once, then at most about once per two seconds.
     * Report before starting the phase, so slow phases are visible too. */
    uint32_t bit = UINT32_C(1) << stage;
    if (!(p->seen & bit) || now - p->last_progress >= INT64_C(2000000)) {
        csi_amp_progress_report_t progress = {
            .stage = stage, .bin = bin, .bins = bins,
            .elapsed_us = now - p->started};
        csi_output_amp_progress(&progress);
        p->last_progress = esp_timer_get_time();
        p->seen |= bit;
    }
    p->active = true;
    p->current = stage;
    p->stage_started = esp_timer_get_time();
}

static int analyze_amplitude_variant(QueueHandle_t queue, const csi_amp_context_t *ctx,
                                       int float_math, size_t max_bins,
                                       const csi_amp_stream_t *stream) {
    int64_t started = esp_timer_get_time(), last_yield = started;
    amplitude_profiler_t profiler = {.started = started};
    csi_amp_raw_source_t source = {
        .read = read_window_record, .user = &s_window,
        .yield = amplitude_yield, .yield_user = &last_yield,
        .trace = amplitude_trace, .trace_user = &profiler,
        .fft_plan = &s_fft_plan, .float_math = float_math, .max_bins = max_bins,
        .prepared = csi_amp_stream_data(stream),
        .prepared_rows = csi_amp_stream_rows(stream)};
#ifdef CONFIG_CSI_RX_AMP_Q15_BENCHMARK
    if (float_math == 2 || float_math == 4) source.q15_plan = &s_q15_plan;
    source.fixed_preprocess = float_math == 3 || float_math == 4;
#endif
    int status = csi_amp_process_raw(&source, s_window.count, ctx, s_amp_result);
    int64_t finished = esp_timer_get_time();
    if (profiler.active) {
        profiler.report.stage_us[profiler.current] +=
            (uint64_t)(finished - profiler.stage_started);
    }
    profiler.report.elapsed_us = finished - started;
    csi_amplitude_report_t report = {
        .status = status, .candidate_bin = -1,
        .elapsed_us = finished - started,
        .queued = uxQueueMessagesWaiting(queue)};
    if (!status) {
        report.accepted = s_amp_result->selected_bin >= 0;
        report.candidate_bin = report.accepted ? s_amp_result->selected_bin
                                              : s_amp_result->best_bin;
        report.n_samples = s_amp_result->n_samples;
        report.n_bins = s_amp_result->n_bins;
        if (report.candidate_bin >= 0) {
            report.candidate = s_amp_result->bins[report.candidate_bin];
        }
    }
    csi_output_amplitude(&report);
    csi_output_amp_profile(&profiler.report);
    return status;
}

static csi_heap_report_t heap_snapshot(uint32_t caps) {
    multi_heap_info_t info;
    heap_caps_get_info(&info, caps);
    return (csi_heap_report_t){
        .allocated = info.total_allocated_bytes, .free = info.total_free_bytes,
        .minimum_free = info.minimum_free_bytes, .largest = info.largest_free_block};
}

static void report_memory(void) {
    csi_memory_report_t report = {
        .internal = heap_snapshot(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
        .psram = heap_snapshot(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)};
    csi_output_memory(&report);
}

#if defined(CONFIG_CSI_RX_AMP_Q15_BENCHMARK) && !defined(CONFIG_CSI_RX_AMP_FIXED_PREPROCESS_BENCHMARK)
static void benchmark_fft_kernel(void) {
    const size_t capacity = CSI_RX_AMP_FFT_CAPACITY;
    float *floats = heap_caps_malloc(2*capacity*sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    int16_t *integers = heap_caps_malloc(2*capacity*sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!floats || !integers) {
        csi_fft_kernel_report_t report = {.mode="allocation", .status=1};
        csi_output_fft_kernel(&report);
        heap_caps_free(floats); heap_caps_free(integers);
        return;
    }
    for (size_t n=2048; n<=capacity; n*=2) {
        for (int inverse=0; inverse<=1; ++inverse) {
            for (int fixed=0; fixed<=1; ++fixed) {
                csi_fft_kernel_report_t report = {.mode=fixed ? "q15" : "float",
                    .n=n, .repeats=4, .inverse=inverse};
                for (size_t rep=0; rep<report.repeats; ++rep) {
                    for (size_t i=0; i<n; ++i) {
                        int16_t value = (int16_t)(((int)(i%31)-15)*100);
                        floats[i]=value; floats[n+i]=0;
                        integers[i]=value; integers[n+i]=0;
                    }
                    unsigned shift;
                    int64_t started = esp_timer_get_time();
                    int status = fixed ? csi_fft_q15(integers, integers+n, n,
                        &s_q15_plan, inverse, &shift) :
                        csi_fft_float_planned(floats, floats+n, n, &s_fft_plan, inverse);
                    report.elapsed_us += esp_timer_get_time()-started;
                    report.status |= status;
                    vTaskDelay(1);
                }
                csi_output_fft_kernel(&report);
            }
        }
    }
    heap_caps_free(floats); heap_caps_free(integers);
}
#endif

#ifdef CONFIG_CSI_RX_AMP_BENCHMARK
static void benchmark_amplitude(QueueHandle_t queue) {
    ESP_ERROR_CHECK(esp_wifi_set_csi(false));
    const struct { const char *name; int single, padding, rate, bins, stream; } variants[] = {
#ifdef CONFIG_CSI_RX_AMP_FIXED_PREPROCESS_BENCHMARK
        {"b1_float",1,4,10,0,0}, {"b1_q15_fft",2,4,10,0,0},
        {"b1_fixed_preprocess",3,4,10,0,0}, {"b1_fixed_both",4,4,10,0,0},
        {"b1_fixed_both_repeat",4,4,10,0,0}, {"b1_float_repeat",1,4,10,0,0}
#elif defined(CONFIG_CSI_RX_AMP_Q15_BENCHMARK)
        {"b0_float",1,4,10,0,0}, {"b0_q15",2,4,10,0,0},
        {"b0_q15_repeat",2,4,10,0,0}, {"b0_float_repeat",1,4,10,0,0}
#else
        {"baseline", 0,4,10,0,0}, {"float_fft_fir",1,4,10,0,0},
        {"padding1",0,1,10,0,0}, {"rate5",0,4,5,0,0},
        {"top16",0,4,10,16,0}, {"stream",0,4,10,0,1},
        {"float_padding1",1,1,10,0,0}, {"float_padding1_rate5",1,1,5,0,0},
        {"combined",1,1,5,16,0}, {"combined_stream",1,1,5,16,1},
        {"baseline_repeat",0,4,10,0,0}
#endif
    };
#ifdef CONFIG_CSI_RX_AMP_Q15_BENCHMARK
    int16_t *q15_table = heap_caps_malloc(CSI_RX_AMP_FFT_CAPACITY * sizeof(*q15_table),
                                         MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    int q15_status = !q15_table || csi_fft_q15_plan_init(&s_q15_plan,
        CSI_RX_AMP_FFT_CAPACITY, q15_table, CSI_RX_AMP_FFT_CAPACITY);
    if (q15_status) {
        csi_benchmark_report_t failed = {.name="b0_setup", .status=1, .equal_baseline=-1};
        csi_output_benchmark_end(&failed);
        heap_caps_free(q15_table);
        csi_output_benchmark_complete();
        return;
    }
#ifndef CONFIG_CSI_RX_AMP_FIXED_PREPROCESS_BENCHMARK
    benchmark_fft_kernel();
#endif
#endif
    csi_amp_result_t *baseline = heap_caps_malloc(sizeof(*baseline),
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uint32_t fingerprint = UINT32_C(2166136261);
    for (size_t i=0; i<s_window.count; ++i) {
        const unsigned char *r = (const unsigned char *)csi_window_at(&s_window,i);
        for (size_t j=0; j<sizeof(csi_record_t); ++j)
            fingerprint = (fingerprint ^ r[j]) * UINT32_C(16777619);
    }
    bool baseline_valid = false;
    for (size_t v=0; v<sizeof(variants)/sizeof(variants[0]); ++v) {
        csi_output_benchmark_begin(variants[v].name, s_window.count, fingerprint);
        csi_benchmark_report_t report = {.name=variants[v].name, .status=1,
                                         .equal_baseline=-1};
        int64_t started = esp_timer_get_time(), last_yield = started;
        csi_amp_context_t ctx = {0};
        csi_amp_stream_t *stream = NULL;
        firwin_config_t cfg = {.target_fs=variants[v].rate, .half_width_s=1, .beta=8};
        if (csi_amp_init(&ctx, &cfg)) goto variant_done;
        ctx.analysis.fft_oversampling = variants[v].padding;
        if (variants[v].stream) {
            stream = csi_amp_stream_create(&ctx, csi_window_at(&s_window,0)->len/2,
                CSI_RX_WINDOW_SECONDS * variants[v].rate + 1, variants[v].single);
            if (!stream) goto variant_done;
            report.stream_bytes = csi_amp_stream_bytes(stream);
            int64_t pre_started = esp_timer_get_time();
            for (size_t i=0; i<s_window.count; ++i) {
                int64_t push_started = esp_timer_get_time();
                int status = csi_amp_stream_push(stream, csi_window_at(&s_window,i));
                int64_t push_us = esp_timer_get_time() - push_started;
                if (push_us > report.max_push_us) report.max_push_us = push_us;
                if (status) goto variant_done;
                amplitude_yield(&last_yield);
            }
            if (csi_amp_stream_finish(stream)) goto variant_done;
            report.preprocess_us = esp_timer_get_time() - pre_started;
        }
        report.status = analyze_amplitude_variant(queue, &ctx, variants[v].single,
                                                   variants[v].bins, stream);
        if (!v && !report.status && baseline) {
            memcpy(baseline, s_amp_result, sizeof(*baseline)); baseline_valid = true;
        }
        if (!report.status && baseline_valid)
            report.equal_baseline = memcmp(baseline, s_amp_result, sizeof(*baseline)) == 0;
variant_done:
        csi_amp_stream_free(stream);
        csi_amp_free(&ctx);
        report.total_us = esp_timer_get_time() - started;
        csi_output_benchmark_end(&report);
        report_memory();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    heap_caps_free(baseline);
#ifdef CONFIG_CSI_RX_AMP_Q15_BENCHMARK
    heap_caps_free(q15_table);
    s_q15_plan = (csi_fft_q15_plan_t){0};
#endif
    csi_output_benchmark_complete();
}
#endif

static void report_pipeline(QueueHandle_t queue, csi_pipeline_state_t state,
                             uint32_t queue_drops, uint32_t rejected) {
    uint32_t span = 0;
    if (s_window.count > 1) {
        span = csi_window_at(&s_window, s_window.count - 1)->timestamp -
               csi_window_at(&s_window, 0)->timestamp;
    }
    csi_pipeline_report_t report = {
        .state = state, .count = s_window.count, .capacity = s_window.capacity,
        .queued = uxQueueMessagesWaiting(queue), .span_ms = span / 1000,
        .queue_drops = queue_drops, .capacity_drops = s_window.capacity_drops,
        .rejected = rejected};
    csi_output_pipeline(&report);
}

static void csi_process_task(void *arg) {
    QueueHandle_t queue = (QueueHandle_t)arg;
    csi_record_t record;
    uint32_t last_timestamp = 0;
    uint32_t queue_drops = 0;
    uint32_t rejected = 0;
    int64_t last_receive_us = 0;
    int64_t last_report_us = esp_timer_get_time();
    int64_t last_heap_report_us = last_report_us;
    bool analysis_done = false;
    report_memory();

    for (;;) {
        BaseType_t received = xQueueReceive(queue, &record, pdMS_TO_TICKS(100));
        int64_t now = esp_timer_get_time();

        if (received == pdTRUE) {
            if (csi_window_push(&s_window, &record)) {
                last_timestamp = record.timestamp;
                last_receive_us = now;
                queue_drops = record.dropped;
            } else {
                ++rejected;
            }
        } else if (s_window.count) {
            /* Advance the window during silence as well as during reception.
             * Clamp before casting so a long idle period cannot wrap the age.
             */
            int64_t idle = now - last_receive_us;
            uint32_t elapsed = idle >= s_window.duration_us
                                   ? s_window.duration_us
                                   : (uint32_t)idle;
            csi_window_expire(&s_window, last_timestamp + elapsed);
        }

        if (now - last_report_us >= INT64_C(1000000)) {
            report_pipeline(queue, analysis_done ? CSI_PIPELINE_DONE
                                                  : CSI_PIPELINE_WAITING,
                            queue_drops, rejected);
            last_report_us = now;
        }
        /* One analysis per boot for the pipeline smoke test. Keep collecting
         * afterwards so queue backlog/drops remain visible. */
        if (!analysis_done && received == pdTRUE &&
            uxQueueMessagesWaiting(queue) == 0 && s_window.count > 1 &&
            (uint32_t)(csi_window_at(&s_window, s_window.count - 1)->timestamp -
                       csi_window_at(&s_window, 0)->timestamp) >=
                s_window.duration_us - CSI_INTERP_MAX_GAP_US) {
            report_pipeline(queue, CSI_PIPELINE_RUNNING, queue_drops, rejected);
            report_memory();
#ifdef CONFIG_CSI_RX_AMP_BENCHMARK
            benchmark_amplitude(queue);
#else
            #ifdef CONFIG_CSI_RX_AMP_FLOAT_MATH
            analyze_amplitude_variant(queue, &s_amp, 1, 0, NULL);
#else
            analyze_amplitude_variant(queue, &s_amp, 0, 0, NULL);
#endif
#endif
            analysis_done = true; /* Also stop after an error: preserve its log. */
            report_memory();
            report_pipeline(queue, CSI_PIPELINE_DONE, queue_drops, rejected);
            now = esp_timer_get_time();
        }
        /* Heap walks are diagnostic work: report every five seconds. */
        if (now - last_heap_report_us >= INT64_C(5000000)) {
            report_memory();
            last_heap_report_us = now;
        }
    }
}

esp_err_t start_csi_process(QueueHandle_t queue) {
    if (!queue) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_task) {
        return ESP_ERR_INVALID_STATE;
    }

    size_t bytes = CSI_RX_WINDOW_CAPACITY * sizeof(csi_record_t);
    csi_record_t *storage =
        heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);

    if (!storage) {
        csi_output_window_allocation(bytes, false);
        return ESP_ERR_NO_MEM;
    }

    if (!csi_window_init(&s_window, storage, CSI_RX_WINDOW_CAPACITY,
                         CSI_RX_WINDOW_SECONDS * UINT32_C(1000000))) {
        heap_caps_free(storage);
        return ESP_ERR_INVALID_ARG;
    }

    s_amp_result = heap_caps_malloc(sizeof(*s_amp_result),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    size_t fft_bytes = CSI_RX_AMP_FFT_CAPACITY * sizeof(*s_fft_twiddles);
    s_fft_twiddles = heap_caps_malloc(fft_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    const firwin_config_t filter = {
        .target_fs = 10, .half_width_s = 1, .beta = 8};
    int64_t fft_started = esp_timer_get_time();
    if (!s_amp_result || !s_fft_twiddles || csi_amp_init(&s_amp, &filter) ||
        csi_fft_plan_init(&s_fft_plan, CSI_RX_AMP_FFT_CAPACITY,
                          s_fft_twiddles, CSI_RX_AMP_FFT_CAPACITY)) {
        heap_caps_free(s_fft_twiddles);
        s_fft_twiddles = NULL;
        s_fft_plan = (csi_fft_plan_t){0};
        heap_caps_free(s_amp_result);
        s_amp_result = NULL;
        csi_amp_free(&s_amp);
        heap_caps_free(storage);
        s_window = (csi_window_t){0};
        return ESP_ERR_NO_MEM;
    }
    s_amp.analysis.fft_oversampling = CONFIG_CSI_RX_AMP_FFT_OVERSAMPLING;
#ifdef CONFIG_CSI_RX_AMP_FLOAT_MATH
    csi_output_amp_config(true, s_amp.analysis.fft_oversampling);
#else
    csi_output_amp_config(false, s_amp.analysis.fft_oversampling);
#endif
    csi_output_fft_ready(s_fft_plan.n, fft_bytes, esp_timer_get_time() - fft_started);

    if (xTaskCreate(csi_process_task, "csi_process", 8192, queue, 2, &s_task) !=
        pdPASS) {
        heap_caps_free(storage);
        heap_caps_free(s_fft_twiddles);
        s_fft_twiddles = NULL;
        s_fft_plan = (csi_fft_plan_t){0};
        heap_caps_free(s_amp_result);
        s_amp_result = NULL;
        csi_amp_free(&s_amp);
        s_window = (csi_window_t){0};
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    csi_output_window_allocation(bytes, true);

    return ESP_OK;
}
