#include "constants_rx.h"
#include "csi_capture.h"
#include "csi_process.h"
#include "esp_err.h"
#include "init_wifi.h"
#include "nvs_flash.h"
#include "queue.h"

static QueueHandle_t s_csi_queue;

void app_main() {
    // init flash
    esp_err_t ret = nvs_flash_init();

    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // init wifi
    esp_now_peer_info_t peer = {
        .channel = CSI_RX_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };

    ESP_ERROR_CHECK(init_wifi_base());
    ESP_ERROR_CHECK(init_wifi_esp_now(peer));

    // init queue & window
    ESP_ERROR_CHECK(create_queue(&s_csi_queue));
    ESP_ERROR_CHECK(start_csi_process(s_csi_queue));
    ESP_ERROR_CHECK(init_wifi_csi(s_csi_queue));
}
