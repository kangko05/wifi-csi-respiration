#pragma once

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/* Creates a queue of csi_record_t values. Pass an unused handle variable;
 * on allocation failure it is set to NULL. The caller owns the queue and
 * may release it with vQueueDelete() once no task or callback uses it. */
esp_err_t create_queue(QueueHandle_t *out_queue);
