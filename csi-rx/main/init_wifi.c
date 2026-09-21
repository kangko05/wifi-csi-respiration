#include "init_wifi.h"

#include "esp_check.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"

#include "constants_rx.h"

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

static const char *TAG = "csi-wifi";

esp_err_t init_wifi_base(void) {
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG,
                        "esp_event_loop_create_default failed");
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "esp_netif_init failed");

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();

    ESP_RETURN_ON_ERROR(esp_wifi_init(&cfg), TAG, "esp_wifi_init failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG,
                        "esp_wifi_set_mode failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_storage(WIFI_STORAGE_RAM), TAG,
                        "esp_wifi_set_storage failed");

    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "esp_wifi_start failed");

    ESP_RETURN_ON_ERROR(esp_wifi_set_band_mode(CSI_RX_WIFI_BAND_MODE), TAG,
                        "esp_wifi_set_band_mode failed");

    wifi_protocols_t protocols = {.ghz_2g = CSI_RX_WIFI_2G_PROTOCOL,
                                  .ghz_5g = CSI_RX_WIFI_5G_PROTOCOL};
    ESP_RETURN_ON_ERROR(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols),
                        TAG, "esp_wifi_set_protocols failed");

    wifi_bandwidths_t bandwidth = {.ghz_2g = CSI_RX_WIFI_2G_BANDWIDTH,
                                   .ghz_5g = CSI_RX_WIFI_5G_BANDWIDTH};
    ESP_RETURN_ON_ERROR(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth),
                        TAG, "esp_wifi_set_bandwidths failed");

    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG,
                        "esp_wifi_set_ps failed");

    if ((CSI_RX_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY &&
         CSI_RX_WIFI_2G_BANDWIDTH == WIFI_BW20) ||
        (CSI_RX_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY &&
         CSI_RX_WIFI_5G_BANDWIDTH == WIFI_BW20)) {
        ESP_RETURN_ON_ERROR(
            esp_wifi_set_channel(CSI_RX_CHANNEL, WIFI_SECOND_CHAN_NONE), TAG,
            "esp_wifi_set_channel failed");
    } else {
        ESP_RETURN_ON_ERROR(
            esp_wifi_set_channel(CSI_RX_CHANNEL, WIFI_SECOND_CHAN_ABOVE), TAG,
            "esp_wifi_set_channel failed");
    }
    return ESP_OK;
}

esp_err_t init_wifi_esp_now(esp_now_peer_info_t peer) {
    ESP_RETURN_ON_ERROR(esp_now_init(), TAG, "esp_now_init failed");
    ESP_RETURN_ON_ERROR(esp_now_set_pmk((uint8_t *)"pmk1234567890123"), TAG,
                        "esp_now_set_pmk failed");

    esp_now_rate_config_t rate_config = {.phymode = CSI_RX_ESPNOW_PHYMODE,
                                         .rate = CSI_RX_ESPNOW_RATE,
                                         .ersu = false,
                                         .dcm = false};

    ESP_RETURN_ON_ERROR(esp_now_add_peer(&peer), TAG,
                        "esp_now_add_peer failed");
    ESP_RETURN_ON_ERROR(
        esp_now_set_peer_rate_config(peer.peer_addr, &rate_config), TAG,
        "esp_now_set_peer_rate_config failed");
    return ESP_OK;
}
