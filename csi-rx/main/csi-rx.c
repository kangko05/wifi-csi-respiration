/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Get Start Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "nvs_flash.h"

#include "esp_csi_gain_ctrl.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"

#include "csi_resp/csi_record.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL 36
#define CONFIG_GAIN_CONTROL 1
#define CONFIG_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS WIFI_BW40
#define CONFIG_WIFI_5G_BANDWIDTHS WIFI_BW40
#define CONFIG_WIFI_2G_PROTOCOL WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL WIFI_PROTOCOL_11N

#define CONFIG_ESP_NOW_PHYMODE WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN 0

#define CSI_FORCE_LLTF 0

/* Capture is decoupled from UART output: the CSI callback only fills a record
 * and pushes it to a queue, and a low-priority task does the (blocking)
 * formatting and printing. Doing the output inline in the callback stalls the
 * WiFi task and drops most of the packets. */
// #define CSI_BUF_MAX \ 512 /* bytes of raw CSI per packet; actual len is
// logged                   \ */
#define CSI_QUEUE_LEN 32

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00,
                                              0x00, 0x00, 0x00};
static const char *TAG = "csi-recv";

// typedef struct {
//     uint32_t seq;          /**< tx-side counter lifted from the payload */
//     uint32_t timestamp;    /**< rx_ctrl.timestamp, hardware capture time */
//     uint32_t dropped;      /**< records lost to queue overflow so far */
//     float compensate_gain; /**< applied by the host, not here */
//     int8_t rssi;
//     int8_t noise_floor;
//     int8_t fft_gain;
//     uint8_t agc_gain;
//     uint8_t channel;
//     uint8_t bb_format;
//     uint8_t first_word_invalid;
//     uint16_t sig_len;
//     uint16_t len;            /**< valid bytes in buf */
//     int8_t buf[CSI_BUF_MAX]; /**< raw, un-compensated CSI */
// } csi_record_t;

static QueueHandle_t s_csi_queue;
static uint32_t s_dropped;

static void wifi_init() {
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {.ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
                                  .ghz_5g = CONFIG_WIFI_5G_PROTOCOL};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {.ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
                                   .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS};
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));

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

    // ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer) {
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE, //  WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false};
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info) {
    if (!info || !info->buf) {
        return;
    }

    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6)) {
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    static int s_count = 0;
    static csi_record_t rec; /* too big for the WiFi task stack */

    float compensate_gain = 1.0f;
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;

#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);

    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline,
                                               &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline,
                                            fft_gain_baseline);
#endif
    }
    /* Reported only; the host applies it in float so the raw int8 survives. */
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain,
                                            fft_gain);
#endif
    s_count++;

    uint16_t len = info->len;
    if (len > CSI_BUF_MAX) {
        len = CSI_BUF_MAX;
    }

    rec.seq = *(uint32_t *)(info->payload + 15);
    rec.timestamp = rx_ctrl->timestamp;
    rec.dropped = s_dropped;
    rec.compensate_gain = compensate_gain;
    rec.rssi = rx_ctrl->rssi;
    rec.noise_floor = rx_ctrl->noise_floor;
    rec.fft_gain = fft_gain;
    rec.agc_gain = agc_gain;
    rec.channel = rx_ctrl->channel;
    rec.bb_format = rx_ctrl->cur_bb_format;
    rec.first_word_invalid = info->first_word_invalid;
    rec.sig_len = rx_ctrl->sig_len;
    rec.len = len;
    memcpy(rec.buf, info->buf, len);

    if (xQueueSend(s_csi_queue, &rec, 0) != pdTRUE) {
        s_dropped++;
    }
}

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* Encodes n bytes into out, which must hold 4*ceil(n/3)+1 chars. */
static void b64_encode(const int8_t *in, size_t n, char *out) {
    size_t i = 0, o = 0;
    while (i + 3 <= n) {
        uint32_t v = ((uint32_t)(uint8_t)in[i] << 16) |
                     ((uint32_t)(uint8_t)in[i + 1] << 8) | (uint8_t)in[i + 2];
        out[o++] = B64[(v >> 18) & 0x3f];
        out[o++] = B64[(v >> 12) & 0x3f];
        out[o++] = B64[(v >> 6) & 0x3f];
        out[o++] = B64[v & 0x3f];
        i += 3;
    }
    if (i < n) {
        uint32_t v = (uint32_t)(uint8_t)in[i] << 16;
        if (i + 1 < n) {
            v |= (uint32_t)(uint8_t)in[i + 1] << 8;
        }
        out[o++] = B64[(v >> 18) & 0x3f];
        out[o++] = B64[(v >> 12) & 0x3f];
        out[o++] = (i + 1 < n) ? B64[(v >> 6) & 0x3f] : '=';
        out[o++] = '=';
    }
    out[o] = '\0';
}

/* Drains the capture queue to UART. Blocking here is fine and intended: back
 * pressure lands on the queue (visible as the dropped counter) instead of on
 * the WiFi task. */
static void csi_output_task(void *arg) {
    static csi_record_t rec;
    static char b64[4 * ((CSI_BUF_MAX + 2) / 3) + 1];
    bool header_done = false;

    for (;;) {
        if (xQueueReceive(s_csi_queue, &rec, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        if (!header_done) {
            ESP_LOGI(TAG, "================ CSI RECV ================");
            ESP_LOGI(TAG, "csi len %u bytes, bb_format %u", rec.len,
                     rec.bb_format);
            printf("type,seq,rssi,noise_floor,fft_gain,agc_gain,channel,"
                   "local_timestamp,sig_len,rx_format,len,first_word,"
                   "compensate_gain,dropped,data\n");
            header_done = true;
        }

        b64_encode(rec.buf, rec.len, b64);
        printf("CSI_DATA,%lu,%d,%d,%d,%u,%u,%lu,%u,%u,%u,%u,%.6f,%lu,%s\n",
               (unsigned long)rec.seq, rec.rssi, rec.noise_floor, rec.fft_gain,
               rec.agc_gain, rec.channel, (unsigned long)rec.timestamp,
               rec.sig_len, rec.bb_format, rec.len, rec.first_word_invalid,
               rec.compensate_gain, (unsigned long)rec.dropped, b64);
    }
}

static void wifi_csi_init() {
    s_csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_record_t));
    ESP_ERROR_CHECK(s_csi_queue ? ESP_OK : ESP_ERR_NO_MEM);
    xTaskCreate(csi_output_task, "csi_out", 4096, NULL, 2, NULL);

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /**< default config */
    wifi_csi_config_t csi_config = {.enable = true,
                                    .acquire_csi_legacy = false,
                                    .acquire_csi_force_lltf = CSI_FORCE_LLTF,
                                    .acquire_csi_ht20 = true,
                                    .acquire_csi_ht40 = true,
                                    .acquire_csi_vht = false,
                                    .acquire_csi_su = false,
                                    .acquire_csi_mu = false,
                                    .acquire_csi_dcm = false,
                                    .acquire_csi_beamformed = false,
                                    .acquire_csi_he_stbc_mode = 2,
                                    .val_scale_cfg = 0,
                                    .dump_ack_en = false,
                                    .reserved = false};

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

void app_main() {
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Initialize ESP-NOW
     *        ESP-NOW protocol see:
     * https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_now.html
     */
    esp_now_peer_info_t peer = {
        .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };

    wifi_esp_now_init(peer);

    // check mac addr
    // uint8_t my_mac[6];
    // if (esp_wifi_get_mac(WIFI_IF_STA, my_mac) == ESP_OK) {
    //     ESP_LOGI(TAG, "mymac = " MACSTR, MAC2STR(my_mac));
    // }

    wifi_csi_init();
}
