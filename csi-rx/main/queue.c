#include "queue.h"

#include "constants_rx.h"
#include "csi_resp/csi_record.h"
#include "esp_err.h"
#include "esp_log.h"

static const char *TAG = "csi-queue";

esp_err_t create_queue(QueueHandle_t *out_queue) {
    if (out_queue == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *out_queue = xQueueCreate(CSI_RX_QUEUE_LEN, sizeof(csi_record_t));
    if (*out_queue == NULL) {
        ESP_LOGE(TAG, "failed to create queue (%u records)",
                 (unsigned)CSI_RX_QUEUE_LEN);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "queue created (%u records, %u bytes per record)",
             (unsigned)CSI_RX_QUEUE_LEN, (unsigned)sizeof(csi_record_t));

    return ESP_OK;
}
