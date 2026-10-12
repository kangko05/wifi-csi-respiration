/*
 * CSI transmitter: sends one ESP-NOW unicast frame every TX_SEND_PERIOD_MS to
 * the receiver. The payload is a little-endian uint32 sequence counter that
 * the receiver uses to detect lost packets.
 *
 * Every TX_REPORT_INTERVAL_S the transmitter logs one "tx_stats" line with
 * the send counts and the measured send period.
 */
#include <inttypes.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "tx_config.h"

#if CONFIG_FREERTOS_HZ != 1000
#error "TX_SEND_PERIOD_MS needs CONFIG_FREERTOS_HZ=1000 (see sdkconfig.defaults)"
#endif

static const char *TAG = "csi-tx";

static const uint8_t s_source_mac[ESP_NOW_ETH_ALEN] = TX_SOURCE_MAC;
static const uint8_t s_receiver_mac[ESP_NOW_ETH_ALEN] = TX_RECEIVER_MAC;

/* Updated from the WiFi task through the send callback. */
static atomic_uint_fast32_t s_delivered;
static atomic_uint_fast32_t s_delivery_failed;

/* Logs the failing call by name and stops. Used for one-time setup calls,
 * where continuing with a half-configured radio would silently produce frames
 * that do not match the recorded sessions. */
#define TX_CHECK(call)                                                         \
    do {                                                                       \
        esp_err_t check_err = (call);                                          \
        if (check_err != ESP_OK) {                                             \
            ESP_LOGE(TAG, "%s failed: %s", #call, esp_err_to_name(check_err)); \
            abort();                                                           \
        }                                                                      \
    } while (0)

/* Per-interval send statistics. Times are esp_timer microseconds. */
typedef struct {
    uint32_t sends;
    uint32_t send_api_errors;
    int64_t period_sum_us;
    int64_t period_min_us;
    int64_t period_max_us;
    uint32_t periods;
    int64_t schedule_lag_max_us; /**< worst lateness vs. t0 + n * period */
} send_stats_t;

static void send_cb(const esp_now_send_info_t *tx_info,
                    esp_now_send_status_t status) {
    (void)tx_info;

    if (status == ESP_NOW_SEND_SUCCESS) {
        atomic_fetch_add(&s_delivered, 1);
    } else {
        atomic_fetch_add(&s_delivery_failed, 1);
    }
}

static void nvs_init(void) {
    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        TX_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }

    TX_CHECK(err);
}

/*
 * Call order follows the esp_wifi.h / esp_now.h attention notes (IDF 6.0.3):
 * - esp_wifi_set_mac: only while the interface is disabled -> before start.
 * - esp_wifi_set_protocols / set_bandwidths: after esp_wifi_init. Protocol is
 *   set first because HT40 is only accepted on an 11N interface.
 * - esp_wifi_set_band_mode / set_channel: need a started WiFi.
 * - esp_now_set_peer_rate_config: after esp_wifi_start and esp_now_init.
 */
static void wifi_init(void) {
    TX_CHECK(esp_netif_init());
    TX_CHECK(esp_event_loop_create_default());

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    TX_CHECK(esp_wifi_init(&init_config));

    TX_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    TX_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    TX_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_source_mac));

    wifi_protocols_t protocols = {
        .ghz_2g = TX_WIFI_PROTOCOL_2G,
        .ghz_5g = TX_WIFI_PROTOCOL_5G,
    };
    TX_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = TX_WIFI_BANDWIDTH_2G,
        .ghz_5g = TX_WIFI_BANDWIDTH_5G,
    };
    TX_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));

    TX_CHECK(esp_wifi_start());

    TX_CHECK(esp_wifi_set_band_mode(TX_WIFI_BAND_MODE));
    TX_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    TX_CHECK(esp_wifi_set_channel(TX_WIFI_CHANNEL, TX_WIFI_SECOND_CHANNEL));

#if TX_MAX_TX_POWER_QUARTER_DBM > 0
    /* Limits after esp_wifi_start take precedence over the PHY init data. */
    TX_CHECK(esp_wifi_set_max_tx_power(TX_MAX_TX_POWER_QUARTER_DBM));
#endif
}

static void espnow_init(void) {
    TX_CHECK(esp_now_init());
    TX_CHECK(esp_now_register_send_cb(send_cb));
    TX_CHECK(esp_now_set_pmk((const uint8_t *)TX_ESPNOW_PMK));

    esp_now_peer_info_t peer = {
        .channel = TX_WIFI_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, s_receiver_mac, ESP_NOW_ETH_ALEN);
    TX_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate_config = {
        .phymode = TX_ESPNOW_PHYMODE,
        .rate = TX_ESPNOW_RATE,
        .ersu = false,
        .dcm = false,
    };
    TX_CHECK(esp_now_set_peer_rate_config(s_receiver_mac, &rate_config));
}

static void log_configuration(void) {
    uint8_t actual_mac[ESP_NOW_ETH_ALEN];
    TX_CHECK(esp_wifi_get_mac(WIFI_IF_STA, actual_mac));

    uint8_t primary_channel = 0;
    wifi_second_chan_t second_channel = WIFI_SECOND_CHAN_NONE;
    TX_CHECK(esp_wifi_get_channel(&primary_channel, &second_channel));

    wifi_bandwidths_t bandwidths = {0};
    TX_CHECK(esp_wifi_get_bandwidths(WIFI_IF_STA, &bandwidths));

    ESP_LOGI(TAG, "================ CSI SEND ================");
    ESP_LOGI(TAG, "tx_config,mac=" MACSTR ",receiver=" MACSTR, MAC2STR(actual_mac),
             MAC2STR(s_receiver_mac));
    int8_t max_tx_power = 0;
    TX_CHECK(esp_wifi_get_max_tx_power(&max_tx_power));

    ESP_LOGI(TAG, "tx_config,channel=%u,second=%d,bw5g=%d,period_ms=%d,tick_hz=%d,max_tx_power_qdbm=%d",
             primary_channel, (int)second_channel, (int)bandwidths.ghz_5g,
             TX_SEND_PERIOD_MS, CONFIG_FREERTOS_HZ, (int)max_tx_power);
}

static void reset_stats(send_stats_t *stats) {
    *stats = (send_stats_t){
        .period_min_us = INT64_MAX,
        .period_max_us = 0,
    };
}

/* One CSV-like line per interval so the log can be parsed later. */
static void report_stats(const send_stats_t *stats, uint32_t seq) {
    uint32_t delivered = atomic_exchange(&s_delivered, 0);
    uint32_t delivery_failed = atomic_exchange(&s_delivery_failed, 0);

    int64_t period_mean_us = 0;

    if (stats->periods > 0) {
        period_mean_us = stats->period_sum_us / stats->periods;
    }

    ESP_LOGI(TAG,
             "tx_stats,seq=%" PRIu32 ",sends=%" PRIu32 ",api_errors=%" PRIu32
             ",delivered=%" PRIu32 ",delivery_failed=%" PRIu32
             ",period_mean_us=%" PRId64 ",period_min_us=%" PRId64
             ",period_max_us=%" PRId64 ",schedule_lag_max_us=%" PRId64
             ",free_heap=%" PRIu32,
             seq, stats->sends, stats->send_api_errors, delivered,
             delivery_failed, period_mean_us, stats->period_min_us,
             stats->period_max_us, stats->schedule_lag_max_us,
             esp_get_free_heap_size());
}

/*
 * vTaskDelayUntil wakes at absolute tick times (last_wake + period), so the
 * time spent in esp_now_send and logging does not accumulate as drift.
 */
static void send_loop(void) {
    const TickType_t period_ticks = pdMS_TO_TICKS(TX_SEND_PERIOD_MS);
    const int64_t period_us = (int64_t)TX_SEND_PERIOD_MS * 1000;
    const uint32_t sends_per_report = TX_REPORT_INTERVAL_S * 1000 / TX_SEND_PERIOD_MS;

    send_stats_t stats;
    reset_stats(&stats);

    TickType_t last_wake = xTaskGetTickCount();
    int64_t start_us = esp_timer_get_time();
    int64_t previous_send_us = 0;

    for (uint32_t seq = 0;; ++seq) {
        int64_t now_us = esp_timer_get_time();

        esp_err_t err = esp_now_send(s_receiver_mac, (const uint8_t *)&seq, sizeof(seq));

        if (err != ESP_OK) {
            stats.send_api_errors++;
        }

        stats.sends++;

        if (seq > 0) {
            int64_t period_measured_us = now_us - previous_send_us;

            stats.period_sum_us += period_measured_us;
            stats.periods++;

            if (period_measured_us < stats.period_min_us) {
                stats.period_min_us = period_measured_us;
            }

            if (period_measured_us > stats.period_max_us) {
                stats.period_max_us = period_measured_us;
            }
        }

        int64_t schedule_lag_us = now_us - (start_us + (int64_t)seq * period_us);

        if (schedule_lag_us > stats.schedule_lag_max_us) {
            stats.schedule_lag_max_us = schedule_lag_us;
        }

        previous_send_us = now_us;

        if (stats.sends == sends_per_report) {
            report_stats(&stats, seq);
            reset_stats(&stats);
        }

        vTaskDelayUntil(&last_wake, period_ticks);
    }
}

void app_main(void) {
    nvs_init();
    wifi_init();
    espnow_init();
    log_configuration();
    send_loop();
}
