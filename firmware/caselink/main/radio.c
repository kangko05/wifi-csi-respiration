#include "radio.h"

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "esp_csi_gain_ctrl.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

#include "ari2_packet.h"
/* relay=3 credentials, kept out of git:
 *   #define LINK_AP_SSID "..."
 *   #define LINK_AP_PASSWORD "..." */
#if __has_include("ap_secret.h")
#include "ap_secret.h"
#else
#define LINK_AP_SSID ""
#define LINK_AP_PASSWORD ""
#endif
#include "frame_out.h"

#define CSI_QUEUE_DEPTH 32
/* Packets recorded before the gain baseline (median) is read. */
#define GAIN_BASELINE_PACKETS 100
/* Room is left for the f32 compensate_gain trailer. */
#define CSI_MAX_BYTES (FRAME_MAX_PAYLOAD - sizeof(csi_record_header_t) - sizeof(float))
#define TX_PAYLOAD_MAGIC 0x314C5343u /* "CSL1" */

/*
 * Relay to the main unit (csi-main in ARI2 relay mode), copied from the old
 * room receiver csi-rx: unicast to the main unit's base MAC on its channel,
 * HT20 MCS0, no encryption. csi-main accepts any 154-byte ESP-NOW frame that
 * starts with "ARI2", whoever sends it.
 */
#define RELAY_MAIN_MAC {0x10, 0xbd, 0xa3, 0xd6, 0x74, 0x64}
#define RELAY_ESPNOW_PMK "pmk1234567890123"
#define RELAY_ESPNOW_RATE WIFI_PHY_RATE_MCS0_LGI
/* ARI2 header: room = link + 1 (link 0 is room 1), node 1 is the in-case receiver. */
#define RELAY_ARI2_ROOM_OFFSET 1
#define RELAY_ARI2_NODE 1
#define RELAY_ARI2_FLAGS 0
/* The receiver cannot read the transmitter's power; 0 means unknown. */
#define RELAY_ARI2_TX_POWER_QDBM 0

/*
 * Offset of the transmitter's uint32 seq inside wifi_csi_info_t.payload. The
 * payload is the ESP-NOW action frame body: category (1), organization id
 * (3), random (4), vendor element id (1), length (1), OUI (3), type (1),
 * version (1) = 15 bytes, then the esp_now_send data, which tx_payload_t
 * starts with seq. csi-main reads the same offset (MAIN_SEQ_PAYLOAD_OFFSET).
 */
#define RELAY_SEQ_PAYLOAD_OFFSET 15

/* relay=3: wait between reconnect attempts; each one scans and takes the
 * radio off the case link for a moment. */
#define AP_RECONNECT_MS 1000

static const char *TAG = "radio";

static const uint8_t s_broadcast_mac[ESP_NOW_ETH_ALEN] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const uint8_t s_main_mac[ESP_NOW_ETH_ALEN] = RELAY_MAIN_MAC;

/* The compensate_gain trailer is written right after csi[csi_length]
 * (see radio.h), so it lives inside the csi buffer, not as a field. tx_seq
 * sits after the buffer: the UART frame is written from the header up to the
 * trailer, so it never reaches the wire. */
typedef struct {
    csi_record_header_t header;
    int8_t csi[CSI_MAX_BYTES + sizeof(float)];
    uint32_t tx_seq;
} csi_record_t;

/* seq comes first: the main unit (csi-main) reads the transmitter's sequence
 * number at a fixed offset in the frame payload, where the old csi-tx put it. */
typedef struct __attribute__((packed)) {
    uint32_t seq;
    uint32_t magic;
} tx_payload_t;

static link_config_t s_config;
static uint8_t s_boot_id;
static uint8_t s_transmitter_mac[ESP_NOW_ETH_ALEN];
static QueueHandle_t s_csi_queue;

static atomic_uint s_rx_count;
static atomic_uint s_rx_dropped;
static atomic_uint s_rx_other_source;
static atomic_uint s_tx_sent;
static atomic_uint s_tx_failed;
static atomic_uint s_relay_sent;
static atomic_uint s_relay_failed;
static atomic_bool s_ap_ready;
static int s_ap_socket = -1;
static struct sockaddr_in s_ap_address;
static esp_timer_handle_t s_ap_reconnect_timer;
static atomic_bool s_gain_forced;
static atomic_bool s_baseline_ready;
static atomic_int s_baseline_agc;
static atomic_int s_baseline_fft;

/* Transmitter MAC is fixed by link_id so the receiver needs no pairing:
 * locally administered 1a:00:00:00:00:<link_id>. */
static void make_transmitter_mac(uint8_t link_id, uint8_t mac[ESP_NOW_ETH_ALEN]) {
    mac[0] = 0x1a;
    mac[1] = 0x00;
    mac[2] = 0x00;
    mac[3] = 0x00;
    mac[4] = 0x00;
    mac[5] = link_id;
}

/*
 * Call order follows the esp_wifi.h attention notes (IDF 6.0.3):
 * set_mac only while the interface is disabled, protocols before bandwidths
 * (HT40 needs 11N), band mode and channel after start.
 */
static void wifi_start(const link_config_t *config) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    if (config->relay == LINK_RELAY_AP) {
        esp_netif_create_default_wifi_sta();
    }

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    if (config->role == LINK_ROLE_TX) {
        ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_transmitter_mac));
    }

    wifi_protocols_t protocols = {
        .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N,
        .ghz_5g = WIFI_PROTOCOL_11A | WIFI_PROTOCOL_11N,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));

    wifi_bandwidth_t bandwidth_5g = WIFI_BW20;
    wifi_second_chan_t second_channel = WIFI_SECOND_CHAN_NONE;

    if (config->bandwidth_mhz == 40) {
        bandwidth_5g = WIFI_BW40;
        second_channel = WIFI_SECOND_CHAN_ABOVE;
    }

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = WIFI_BW20,
        .ghz_5g = bandwidth_5g,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));

    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_5G_ONLY));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(config->channel, second_channel));

    if (config->tx_power_qdbm > 0) {
        /* Limits after esp_wifi_start take precedence over the PHY init data. */
        ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(config->tx_power_qdbm));
    }
}

/* ---------------------------------------------------------------- transmitter */

static void tx_task(void *argument) {
    (void)argument;

    TickType_t period_ticks = pdMS_TO_TICKS(s_config.period_ms);
    TickType_t last_wake = xTaskGetTickCount();
    tx_payload_t payload = {
        .magic = TX_PAYLOAD_MAGIC,
        .seq = 0,
    };

    while (true) {
        /* Absolute schedule: time spent in esp_now_send does not accumulate as drift. */
        vTaskDelayUntil(&last_wake, period_ticks);

        esp_err_t err = esp_now_send(s_broadcast_mac, (const uint8_t *)&payload, sizeof(payload));

        if (err == ESP_OK) {
            atomic_fetch_add(&s_tx_sent, 1);
        } else {
            atomic_fetch_add(&s_tx_failed, 1);
        }

        payload.seq++;
    }
}

static void transmitter_start(void) {
    ESP_ERROR_CHECK(esp_now_init());

    esp_now_peer_info_t peer = {
        .channel = s_config.channel,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, s_broadcast_mac, ESP_NOW_ETH_ALEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    wifi_phy_mode_t phy_mode = WIFI_PHY_MODE_HT20;

    if (s_config.bandwidth_mhz == 40) {
        phy_mode = WIFI_PHY_MODE_HT40;
    }

    esp_now_rate_config_t rate_config = {
        .phymode = phy_mode,
        .rate = (wifi_phy_rate_t)s_config.phy_rate,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(s_broadcast_mac, &rate_config));

    xTaskCreate(tx_task, "tx", 4096, NULL, 10, NULL);
}

/* ------------------------------------------------------------------- receiver */

/* Forces the pair stored in the config. Called from the first CSI callback,
 * not from receiver_start: forcing before the RX path has seen a frame did
 * not hold on the board (the AGC kept hunting between two values), while
 * forcing from inside the callback is the path the measured baseline uses. */
static bool force_stored_gain(void) {
    if (esp_csi_gain_ctrl_set_rx_force_gain(s_config.forced_agc, s_config.forced_fft) != ESP_OK) {
        return false;
    }

    atomic_store(&s_gain_forced, true);
    atomic_store(&s_baseline_agc, s_config.forced_agc);
    atomic_store(&s_baseline_fft, s_config.forced_fft);
    atomic_store(&s_baseline_ready, true);

    return true;
}

/* Measures the gain baseline from the first packets in both gain modes, so
 * compensate_gain can be reported with auto gain too; with gain=force the
 * baseline is also forced. With a stored pair the measurement is skipped and
 * the pair is forced on the first packet. Returns true while the baseline is
 * usable. */
static bool update_gain_baseline(uint8_t agc_gain, int8_t fft_gain) {
    if (atomic_load(&s_baseline_ready)) {
        return true;
    }

    bool forced_gain_stored = s_config.forced_agc != 0 || s_config.forced_fft != 0;

    if (s_config.force_gain && forced_gain_stored) {
        return force_stored_gain();
    }

    static uint32_t recorded_packets = 0;

    esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    recorded_packets++;

    if (recorded_packets < GAIN_BASELINE_PACKETS) {
        return false;
    }

    uint8_t baseline_agc = 0;
    int8_t baseline_fft = 0;

    if (esp_csi_gain_ctrl_get_rx_gain_baseline(&baseline_agc, &baseline_fft) != ESP_OK) {
        return false;
    }

    /* (0, 0) means "release forced gain" to set_rx_force_gain, so it can
     * never be a baseline; keep collecting. */
    if (baseline_agc == 0 && baseline_fft == 0) {
        return false;
    }

    if (s_config.force_gain) {
        if (esp_csi_gain_ctrl_set_rx_force_gain(baseline_agc, baseline_fft) != ESP_OK) {
            return false;
        }

        atomic_store(&s_gain_forced, true);
    }

    atomic_store(&s_baseline_agc, baseline_agc);
    atomic_store(&s_baseline_fft, baseline_fft);
    atomic_store(&s_baseline_ready, true);

    return true;
}

/* Runs in the Wi-Fi task: copy and hand off, never block. */
static void csi_callback(void *context, wifi_csi_info_t *info) {
    (void)context;

    if (info == NULL || info->buf == NULL) {
        return;
    }

    if (memcmp(info->mac, s_transmitter_mac, ESP_NOW_ETH_ALEN) != 0) {
        atomic_fetch_add(&s_rx_other_source, 1);
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
    bool baseline_ready = update_gain_baseline(agc_gain, fft_gain);

    float compensate_gain = 1.0f;
    bool compensate_valid = false;

    if (baseline_ready) {
        compensate_valid = esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain) == ESP_OK;
    }

    uint16_t csi_length = info->len;

    if (csi_length > CSI_MAX_BYTES) {
        csi_length = CSI_MAX_BYTES;
    }

    uint8_t flags = 0;

    if (info->first_word_invalid) {
        flags |= CSI_FLAG_FIRST_WORD_INVALID;
    }

    if (atomic_load(&s_gain_forced)) {
        flags |= CSI_FLAG_GAIN_FORCED;
    }

    if (rx_ctrl->rx_channel_estimate_info_vld) {
        flags |= CSI_FLAG_ESTIMATE_VALID;
    }

    if (compensate_valid) {
        flags |= CSI_FLAG_COMPENSATE_VALID;
    }

    /* Static: ~1 KiB is too much for the Wi-Fi task stack, and this callback
     * only ever runs in that one task. */
    static csi_record_t record;
    record.header.rx_count = atomic_fetch_add(&s_rx_count, 1);
    record.header.timestamp_us = rx_ctrl->timestamp;
    record.header.rx_seq = info->rx_seq;
    record.header.rssi = rx_ctrl->rssi;
    record.header.noise_floor = rx_ctrl->noise_floor;
    record.header.agc_gain = agc_gain;
    record.header.fft_gain = fft_gain;
    record.header.channel = rx_ctrl->channel;
    record.header.second = rx_ctrl->second;
    record.header.bb_format = rx_ctrl->cur_bb_format;
    record.header.rate = rx_ctrl->rate;
    record.header.flags = flags;
    record.header.boot_id = s_boot_id;
    record.header.csi_length = csi_length;
    memcpy(record.csi, info->buf, csi_length);
    memcpy(record.csi + csi_length, &compensate_gain, sizeof(compensate_gain));

    /* Falls back to rx_count for a payload too short to hold the seq, so the
     * ARI2 sequence still counts up. */
    record.tx_seq = record.header.rx_count;

    if (info->payload != NULL && info->payload_len >= RELAY_SEQ_PAYLOAD_OFFSET + sizeof(record.tx_seq)) {
        memcpy(&record.tx_seq, info->payload + RELAY_SEQ_PAYLOAD_OFFSET, sizeof(record.tx_seq));
    }

    if (xQueueSend(s_csi_queue, &record, 0) != pdTRUE) {
        atomic_fetch_add(&s_rx_dropped, 1);
    }
}

/* Same rules as csi-rx: only HT20 HT-LTF records (57 pairs) with a valid
 * channel estimate become ARI2 packets; anything else is skipped silently. */
static bool relay_build_packet(const csi_record_t *record, uint8_t packet[ARI2_HT20_PACKET_BYTES]) {
    if ((record->header.flags & CSI_FLAG_ESTIMATE_VALID) == 0) {
        return false;
    }

    bool first_word_invalid = (record->header.flags & CSI_FLAG_FIRST_WORD_INVALID) != 0;
    int8_t iq[ARI2_HT20_IQ_BYTES];
    bool mapped = ari2_map_ht20(record->csi, record->header.csi_length, first_word_invalid,
                                ARI2_SOURCE_ASCENDING, iq);

    if (!mapped) {
        return false;
    }

    /* The TX seq serves as both sequence numbers: one CSI sample per
     * transmitted packet. */
    ari2_header_t header = {
        .room = (uint8_t)(s_config.link_id + RELAY_ARI2_ROOM_OFFSET),
        .node = RELAY_ARI2_NODE,
        .channel = record->header.channel,
        .flags = RELAY_ARI2_FLAGS,
        .boot_id = s_boot_id,
        .packet_seq = record->tx_seq,
        .sample_seq = record->tx_seq,
        .timestamp_us = record->header.timestamp_us,
        .rssi = record->header.rssi,
        .agc_gain = record->header.agc_gain,
        .fft_gain = record->header.fft_gain,
        .tx_power_quarter_dbm = RELAY_ARI2_TX_POWER_QDBM,
    };
    ari2_pack_ht20(&header, iq, packet);

    return true;
}

/* esp_now_send copies the payload. A failed send is not retried: the PC sees
 * the gap in packet_seq. */
static void relay_send(const csi_record_t *record) {
    static uint8_t packet[ARI2_HT20_PACKET_BYTES];

    if (!relay_build_packet(record, packet)) {
        return;
    }

    /* relay=2 broadcasts: a unicast frame needs the main unit's ACK, which a
     * receiver with its gain forced low for the few-cm case link may not
     * decode, so every send waits for retries and the queue fills. */
    if (s_config.relay == LINK_RELAY_AP) {
        if (!atomic_load(&s_ap_ready)) {
            atomic_fetch_add(&s_relay_failed, 1);
        } else if (sendto(s_ap_socket, packet, sizeof(packet), 0,
                          (const struct sockaddr *)&s_ap_address, sizeof(s_ap_address)) < 0) {
            atomic_fetch_add(&s_relay_failed, 1);
        } else {
            atomic_fetch_add(&s_relay_sent, 1);
        }
        return;
    }

    const uint8_t *destination = s_config.relay == 2 ? s_broadcast_mac : s_main_mac;

    if (esp_now_send(destination, packet, sizeof(packet)) == ESP_OK) {
        atomic_fetch_add(&s_relay_sent, 1);
    } else {
        atomic_fetch_add(&s_relay_failed, 1);
    }
}

static void csi_writer_task(void *argument) {
    (void)argument;

    static csi_record_t record;

    while (true) {
        xQueueReceive(s_csi_queue, &record, portMAX_DELAY);

        uint16_t length = sizeof(record.header) + record.header.csi_length + sizeof(float);
        frame_out_write(FRAME_TYPE_CSI, &record, length);

        if (s_config.relay) {
            relay_send(&record);
        }
    }
}

/* ---------------------------------------------------------------- AP relay */

static void ap_reconnect(void *argument) {
    (void)argument;

    esp_wifi_connect();
}

static void ap_event_handler(void *argument, esp_event_base_t base, int32_t id, void *data) {
    (void)argument;
    (void)data;

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        atomic_store(&s_ap_ready, false);
        esp_timer_stop(s_ap_reconnect_timer);
        esp_timer_start_once(s_ap_reconnect_timer, (uint64_t)AP_RECONNECT_MS * 1000);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        atomic_store(&s_ap_ready, true);
    }
}

/* The station follows the AP's channel once connected, so the case link only
 * works when ch is the AP's channel. */
static void ap_relay_start(void) {
    if (LINK_AP_SSID[0] == '\0') {
        ESP_LOGE(TAG, "relay=3 without main/ap_secret.h: no AP to join");
        return;
    }

    s_ap_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);

    if (s_ap_socket < 0) {
        ESP_LOGE(TAG, "udp socket failed");
        abort();
    }

    s_ap_address.sin_family = AF_INET;
    s_ap_address.sin_port = htons(CONFIG_LINK_AP_PORT);

    if (inet_pton(AF_INET, CONFIG_LINK_AP_HOST, &s_ap_address.sin_addr) != 1) {
        ESP_LOGE(TAG, "bad CONFIG_LINK_AP_HOST %s", CONFIG_LINK_AP_HOST);
        abort();
    }

    const esp_timer_create_args_t timer_args = {
        .callback = ap_reconnect,
        .name = "ap_reconnect",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_ap_reconnect_timer));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                                               ap_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, ap_event_handler,
                                               NULL));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, LINK_AP_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, LINK_AP_PASSWORD,
            sizeof(wifi_config.sta.password));
    wifi_config.sta.channel = s_config.channel;
    wifi_config.sta.scan_method = WIFI_FAST_SCAN;
    wifi_config.sta.threshold.authmode =
        LINK_AP_PASSWORD[0] != '\0' ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_connect());
    ESP_LOGI(TAG, "ap relay: ssid=%s -> %s:%d", LINK_AP_SSID, CONFIG_LINK_AP_HOST,
             CONFIG_LINK_AP_PORT);
}

/* ESP-NOW is set up after esp_wifi_start and before promiscuous mode and
 * CSI, the order csi-rx used while it both received CSI and sent. */
static void relay_start(void) {
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((const uint8_t *)RELAY_ESPNOW_PMK));

    esp_now_peer_info_t main_peer = {
        .channel = s_config.channel,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(main_peer.peer_addr, s_main_mac, ESP_NOW_ETH_ALEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&main_peer));

    esp_now_peer_info_t broadcast_peer = main_peer;
    memcpy(broadcast_peer.peer_addr, s_broadcast_mac, ESP_NOW_ETH_ALEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&broadcast_peer));

    esp_now_rate_config_t rate_config = {
        .phymode = WIFI_PHY_MODE_HT20,
        .rate = RELAY_ESPNOW_RATE,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(s_main_mac, &rate_config));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(s_broadcast_mac, &rate_config));
}

static void receiver_start(void) {
    s_csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_record_t));

    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "csi queue allocation failed");
        abort();
    }

    xTaskCreate(csi_writer_task, "csi_out", 4096, NULL, 9, NULL);

    if (s_config.relay == LINK_RELAY_AP) {
        ap_relay_start();
    } else if (s_config.relay) {
        relay_start();
    }

    /* ESP-NOW frames are action (management) frames. */
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT,
    };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    wifi_csi_config_t csi_config = {
        .enable = true,
        .acquire_csi_legacy = s_config.acquire_lltf,
        .acquire_csi_force_lltf = false,
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
        .reserved = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

/* --------------------------------------------------------------------- public */

void radio_start(const link_config_t *config, uint8_t boot_id) {
    s_config = *config;
    s_boot_id = boot_id;
    make_transmitter_mac(config->link_id, s_transmitter_mac);

    if (config->role == LINK_ROLE_IDLE) {
        return;
    }

    wifi_start(config);

    if (config->role == LINK_ROLE_TX) {
        transmitter_start();
    } else {
        receiver_start();
    }

    uint8_t own_mac[ESP_NOW_ETH_ALEN];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, own_mac));

    uint8_t primary_channel = 0;
    wifi_second_chan_t second_channel = WIFI_SECOND_CHAN_NONE;
    ESP_ERROR_CHECK(esp_wifi_get_channel(&primary_channel, &second_channel));

    int8_t max_tx_power = 0;
    ESP_ERROR_CHECK(esp_wifi_get_max_tx_power(&max_tx_power));

    frame_out_text("radio mac=" MACSTR " transmitter=" MACSTR " channel=%u second=%d max_tx_power_qdbm=%d",
                   MAC2STR(own_mac), MAC2STR(s_transmitter_mac), primary_channel, (int)second_channel,
                   max_tx_power);
}

bool radio_get_measured_forced_gain(uint8_t *agc_gain, int8_t *fft_gain) {
    bool forced_gain_stored = s_config.forced_agc != 0 || s_config.forced_fft != 0;

    if (!s_config.force_gain || forced_gain_stored) {
        return false;
    }

    if (!atomic_load(&s_baseline_ready)) {
        return false;
    }

    *agc_gain = (uint8_t)atomic_load(&s_baseline_agc);
    *fft_gain = (int8_t)atomic_load(&s_baseline_fft);
    return true;
}

void radio_format_status(char *text, int text_size) {
    snprintf(text, text_size,
             "status role=%s boot_id=%u tx_sent=%u tx_failed=%u rx=%u rx_dropped=%u rx_other=%u relay=%u relay_failed=%u gain_forced=%d baseline_agc=%d baseline_fft=%d",
             link_role_name(s_config.role),
             s_boot_id,
             atomic_load(&s_tx_sent),
             atomic_load(&s_tx_failed),
             atomic_load(&s_rx_count),
             atomic_load(&s_rx_dropped),
             atomic_load(&s_rx_other_source),
             atomic_load(&s_relay_sent),
             atomic_load(&s_relay_failed),
             (int)atomic_load(&s_gain_forced),
             atomic_load(&s_baseline_agc),
             atomic_load(&s_baseline_fft));
}
