#include "csi_process.h"

#include <inttypes.h>

#include "constants_rx.h"
#include "csi_window.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"

static const char *TAG = "csi-process";
static csi_window_t s_window;
static TaskHandle_t s_task;

static void csi_process_task(void *arg) {
    QueueHandle_t queue = (QueueHandle_t)arg;
    csi_record_t record;
    uint32_t last_timestamp = 0;
    uint32_t queue_drops = 0;
    uint32_t rejected = 0;
    int64_t last_receive_us = 0;
    int64_t last_report_us = esp_timer_get_time();

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
            uint32_t span = 0;
            if (s_window.count > 1) {
                span = csi_window_at(&s_window, s_window.count - 1)->timestamp -
                       csi_window_at(&s_window, 0)->timestamp;
            }
            ESP_LOGI(TAG,
                     "window=%u/%u span=%" PRIu32 "ms queued=%u "
                     "queue_drops=%" PRIu32 " capacity_drops=%" PRIu32
                     " rejected=%" PRIu32,
                     (unsigned)s_window.count, (unsigned)s_window.capacity,
                     span / 1000, (unsigned)uxQueueMessagesWaiting(queue),
                     queue_drops, s_window.capacity_drops, rejected);
            /* Future respiration estimation belongs here, in this same task.
             * No other task may read or mutate s_window concurrently. */
            last_report_us = now;
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
        ESP_LOGE(TAG,
                 "60s window needs %u bytes of contiguous PSRAM; "
                 "check PSRAM configuration and free memory",
                 (unsigned)bytes);
        return ESP_ERR_NO_MEM;
    }

    if (!csi_window_init(&s_window, storage, CSI_RX_WINDOW_CAPACITY,
                         CSI_RX_WINDOW_SECONDS * UINT32_C(1000000))) {
        heap_caps_free(storage);
        return ESP_ERR_INVALID_ARG;
    }

    if (xTaskCreate(csi_process_task, "csi_process", 4096, queue, 2, &s_task) !=
        pdPASS) {
        heap_caps_free(storage);
        s_window = (csi_window_t){0};
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "raw window allocated: %u bytes in PSRAM", (unsigned)bytes);

    return ESP_OK;
}
