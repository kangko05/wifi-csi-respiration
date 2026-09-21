#include "csi_capture.h"

#include <string.h>

#include "constants_rx.h"
#include "csi_resp/csi_record.h"
#include "esp_check.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_wifi.h"

static const char *TAG = "csi-capture";
/* Must match the source MAC configured by csi-tx. */
static const uint8_t CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
/* TX counter offset in the ESP-NOW frame payload used by this project. */
enum { CSI_SEQ_OFFSET = 15, CSI_GAIN_BASELINE_SAMPLES = 100 };

/* Only the Wi-Fi callback accesses this state. */
static uint32_t s_dropped;
static unsigned s_gain_samples;
static csi_record_t s_record;

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info) {
    QueueHandle_t queue = (QueueHandle_t)ctx;
    if (!queue || !info || !info->buf || !info->payload) {
        return;
    }
    if (memcmp(info->mac, CSI_SEND_MAC, sizeof(CSI_SEND_MAC)) != 0) {
        return;
    }
    if (info->len == 0 || info->len > CSI_BUF_MAX || (info->len % 2) != 0 ||
        info->payload_len < CSI_SEQ_OFFSET + sizeof(s_record.seq)) {
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    /* Queue copies the entire record, including bytes beyond the valid length. */
    memset(&s_record, 0, sizeof(s_record));
    s_record.compensate_gain = 1.0f;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &s_record.agc_gain,
                                 &s_record.fft_gain);
    if (s_gain_samples < CSI_GAIN_BASELINE_SAMPLES) {
        esp_csi_gain_ctrl_record_rx_gain(s_record.agc_gain, s_record.fft_gain);
        ++s_gain_samples;
    } else if (s_gain_samples == CSI_GAIN_BASELINE_SAMPLES) {
        uint8_t agc_baseline;
        int8_t fft_baseline;
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_baseline, &fft_baseline);
        ++s_gain_samples;
    }
    /* Preserve raw int8 I/Q; the analysis stage applies the gain factor. */
    esp_csi_gain_ctrl_get_gain_compensation(&s_record.compensate_gain,
                                           s_record.agc_gain, s_record.fft_gain);

    /* memcpy avoids an unaligned uint32_t read at offset 15. */
    memcpy(&s_record.seq, info->payload + CSI_SEQ_OFFSET, sizeof(s_record.seq));
    s_record.timestamp = rx_ctrl->timestamp;
    s_record.dropped = s_dropped;
    s_record.rssi = rx_ctrl->rssi;
    s_record.noise_floor = rx_ctrl->noise_floor;
    s_record.channel = rx_ctrl->channel;
    s_record.bb_format = rx_ctrl->cur_bb_format;
    s_record.first_word_invalid = info->first_word_invalid;
    s_record.sig_len = rx_ctrl->sig_len;
    s_record.len = info->len;
    memcpy(s_record.buf, info->buf, info->len);

    /* Never wait in the Wi-Fi task; the next queued record reports any drops. */
    if (xQueueSend(queue, &s_record, 0) != pdTRUE) {
        ++s_dropped;
    }
}

esp_err_t init_wifi_csi(QueueHandle_t csi_queue) {
    if (!csi_queue) {
        return ESP_ERR_INVALID_ARG;
    }

    ESP_RETURN_ON_ERROR(esp_wifi_set_promiscuous(true), TAG,
                        "wifi set promiscuous failed");

    /**< default config */
    wifi_csi_config_t csi_config = {.enable = true,
                                    .acquire_csi_legacy = false,
                                    .acquire_csi_force_lltf = CSI_RX_FORCE_LLTF,
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

    ESP_RETURN_ON_ERROR(esp_wifi_set_csi_config(&csi_config), TAG,
                        "wifi csi set config failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, csi_queue), TAG,
                        "wifi set csi cb failed");

    return esp_wifi_set_csi(true);
}
