#pragma once

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/* Call once after Wi-Fi startup and consumer setup, with a queue created by
 * create_queue(). The callback copies csi_record_t values without waiting.
 * Keep the queue alive while capture is enabled. Partial initialization is
 * not rolled back on error. */
esp_err_t init_wifi_csi(QueueHandle_t csi_queue);
