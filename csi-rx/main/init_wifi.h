#pragma once

#include "esp_err.h"
#include "esp_now.h"

/* Returns the first initialization error. Partial initialization is not rolled
 * back; the caller must handle failure before attempting to initialize again.
 */
esp_err_t init_wifi_base(void);
esp_err_t init_wifi_esp_now(esp_now_peer_info_t peer);
