#pragma once

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/* Allocates the raw window in PSRAM and starts its sole owner/consumer.
 * Call once before enabling CSI. Queue must remain alive for the task lifetime. */
esp_err_t start_csi_process(QueueHandle_t queue);
