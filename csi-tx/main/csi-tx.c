#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_wifi_types_generic.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL 36

#define CONFIG_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY

// #define CONFIG_WIFI_2G_BANDWIDTHS WIFI_BW_HT40
// #define CONFIG_WIFI_5G_BANDWIDTHS WIFI_BW_HT40

#define CONFIG_WIFI_2G_BANDWIDTHS WIFI_BW40
#define CONFIG_WIFI_5G_BANDWIDTHS WIFI_BW40

#define CONFIG_WIFI_2G_PROTOCOL WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL WIFI_PROTOCOL_11N

#define CONFIG_ESP_NOW_PHYMODE WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE WIFI_PHY_RATE_MCS0_LGI
/* Breathing lives in 0.05-1.0 Hz, so even 50 Hz is ample for bandwidth. The
 * reason to go higher is coherent integration: the estimator sums P packets,
 * so signal grows as P and noise as sqrt(P) -- doubling the rate buys about
 * 1.5 dB. Measured UART headroom at 100 Hz is ~41% of 921600 baud, and the
 * queue refactor removed the packet loss that made 100 Hz unusable before.
 *
 * Needs CONFIG_FREERTOS_HZ=1000, otherwise the 10 ms period lands on exactly
 * one scheduler tick and the send time jitters by a full period. */
#define CONFIG_SEND_FREQUENCY 100

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

// INFO: target mac 10:bd:a3:d6:5f:40
#define TARGET_MAC_ADDR {0x10, 0xbd, 0xa3, 0xd6, 0x5f, 0x40}

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00,
                                              0x00, 0x00, 0x00};

static const char *TAG = "csi-tx";

static void wifi_init() {
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);

    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL,
    };

    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidths));

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY &&
         CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW20) ||
        (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY &&
         CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                             WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                             WIFI_SECOND_CHAN_ABOVE));
    }

    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer) {
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {.phymode = CONFIG_ESP_NOW_PHYMODE,
                                         .rate = CONFIG_ESP_NOW_RATE,
                                         .ersu = false,
                                         .dcm = false};
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

    // INFO: set tx power ==============================
    // ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(32)); // 8dbm
}

void app_main(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    wifi_init();

    // INFO: target mac 10:bd:a3:d6:5f:40
    esp_now_peer_info_t peer = {
        .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
        .peer_addr = TARGET_MAC_ADDR,
    };
    wifi_esp_now_init(peer);

    ESP_LOGI(TAG, "================ CSI SEND ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d, mac: " MACSTR,
             CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY,
             MAC2STR(CONFIG_CSI_SEND_MAC));

    /* vTaskDelayUntil rather than a sleep after the send, so the send period
     * doesn't drift by however long esp_now_send took. */
    const TickType_t period = pdMS_TO_TICKS(1000 / CONFIG_SEND_FREQUENCY);
    TickType_t last_wake = xTaskGetTickCount();

    for (uint32_t count = 0;; ++count) {
        esp_err_t ret = esp_now_send(peer.peer_addr, (const uint8_t *)&count,
                                     sizeof(count));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "free_heap: %ld <%s> ESP-NOW send error",
                     esp_get_free_heap_size(), esp_err_to_name(ret));
        }

        vTaskDelayUntil(&last_wake, period);
    }
}
