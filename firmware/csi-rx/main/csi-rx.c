/*
 * CSI receiver: binary ARI2 stream for the PC, or text lines with onboard
 * occupancy decisions (stage 1). RX_OUTPUT_MODE (rx_config.h) selects one.
 *
 * The CSI callback runs in the WiFi task. It only filters by the transmitter
 * MAC, copies a compact record and queues it (no blocking, drops counted).
 * A separate processing task turns the records into output.
 *
 * RX_OUTPUT_ARI2 (report 0014): one 154-byte ARI2 packet per accepted HT20
 * frame, written raw to the console UART. Logging is switched off before the
 * first packet, so the byte stream holds only whole packets after the boot
 * log.
 *
 * RX_OUTPUT_TEXT: the task feeds the records to the occupancy decision core
 * and prints, one message per line (docs/spec/0002-output-messages.md):
 *   CSI_DATA    raw record (optional, RX_RAW_CSI_OUTPUT)
 *   OCC_STATS   one closed 1 s window
 *   OCC_EVENT   occupancy state change
 *   OCC_HEALTH  heap, stack, queue and link status every RX_HEALTH_INTERVAL_S
 *
 * With RX_REPORT_TO_MAIN the OCC_STATS and OCC_EVENT lines are also sent to
 * the main unit, so a receiver in a room needs power only.
 *
 * The processing order per record is the one the PC replay uses (raw line,
 * advance_time, push, poll), so a replay of the raw lines reproduces the
 * decisions (ticket 08).
 */
#include <inttypes.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "driver/uart.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_random.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "ari2_packet.h"
#include "occupancy_core.h"
#include "rx_config.h"

static const char *TAG = "csi-rx";

static const uint8_t s_transmitter_mac[ESP_NOW_ETH_ALEN] = RX_TRANSMITTER_MAC;

/* Packets used to estimate the gain baseline for compensate_gain. */
#define GAIN_BASELINE_PACKETS 100

/* Logs the failing call by name and stops (setup calls only). */
#define RX_CHECK(call)                                                         \
    do {                                                                       \
        esp_err_t check_err = (call);                                          \
        if (check_err != ESP_OK) {                                             \
            ESP_LOGE(TAG, "%s failed: %s", #call, esp_err_to_name(check_err)); \
            abort();                                                           \
        }                                                                      \
    } while (0)

/* One received packet, as queued from the callback. */
typedef struct {
    uint32_t seq;
    uint32_t hardware_timestamp_us; /**< rx_ctrl.timestamp, wraps at 2^32 */
    int64_t queued_at_us;           /**< esp_timer time in the callback */
    float compensate_gain;          /**< reported only, never applied */
    int8_t rssi;
    int8_t noise_floor;
    int8_t fft_gain;
    uint8_t agc_gain;
    uint8_t channel;
    uint8_t rx_format;
    uint8_t first_word_invalid;
    uint8_t channel_estimate_valid; /**< rx_ctrl.rx_channel_estimate_info_vld */
    uint16_t sig_len;
    uint16_t len;
    int8_t csi[RX_CSI_MAX_BYTES];
} rx_record_t;

static QueueHandle_t s_queue;
static TaskHandle_t s_processing_task;

#if RX_REPORT_TO_MAIN || RX_ARI2_SEND_TO_MAIN

static const uint8_t s_main_mac[ESP_NOW_ETH_ALEN] = RX_MAIN_MAC;

/* Broadcast destination (RX_ARI2_SEND_BROADCAST): already an ESP-NOW peer, see
 * espnow_init. */
static const uint8_t s_broadcast_mac[ESP_NOW_ETH_ALEN] = {0xff, 0xff, 0xff,
                                                          0xff, 0xff, 0xff};

#endif

static atomic_uint_fast32_t s_queue_drops;
static atomic_uint_fast32_t s_callback_records;

/* Gain baseline result, written once by the callback, printed by the task. */
static atomic_bool s_gain_report_ready;
static uint8_t s_baseline_agc;
static int8_t s_baseline_fft;
static esp_err_t s_baseline_err;
static esp_err_t s_force_err;

/* ------------------------------------------------------------- callback */

static void csi_callback(void *context, wifi_csi_info_t *info) {
    (void)context;

    if (info == NULL || info->buf == NULL) {
        return;
    }

    if (memcmp(info->mac, s_transmitter_mac, ESP_NOW_ETH_ALEN) != 0) {
        return;
    }

    if (info->payload == NULL ||
        info->payload_len < RX_SEQ_PAYLOAD_OFFSET + sizeof(uint32_t)) {
        return;
    }

    /* Static: too large for the WiFi task stack, and only this callback uses
     * it. */
    static rx_record_t record;
    static uint32_t packet_count;
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    float compensate_gain = 1.0f;

    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);

    if (packet_count < GAIN_BASELINE_PACKETS) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else {
        if (packet_count == GAIN_BASELINE_PACKETS) {
            s_baseline_err = esp_csi_gain_ctrl_get_rx_gain_baseline(
                &s_baseline_agc, &s_baseline_fft);
            s_force_err = ESP_OK;

#if RX_FORCE_RX_GAIN
            if (s_baseline_err == ESP_OK) {
                s_force_err = esp_csi_gain_ctrl_set_rx_force_gain(
                    s_baseline_agc, s_baseline_fft);
            }
#endif

            atomic_store(&s_gain_report_ready, true);
        }

        esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain,
                                                fft_gain);
    }

    packet_count++;

    uint16_t len = info->len;

    if (len > RX_CSI_MAX_BYTES) {
        len = RX_CSI_MAX_BYTES;
    }

    memcpy(&record.seq, info->payload + RX_SEQ_PAYLOAD_OFFSET,
           sizeof(record.seq));
    record.hardware_timestamp_us = rx_ctrl->timestamp;
    record.queued_at_us = esp_timer_get_time();
    record.compensate_gain = compensate_gain;
    record.rssi = rx_ctrl->rssi;
    record.noise_floor = rx_ctrl->noise_floor;
    record.fft_gain = fft_gain;
    record.agc_gain = agc_gain;
    record.channel = rx_ctrl->channel;
    record.rx_format = rx_ctrl->cur_bb_format;
    record.first_word_invalid = info->first_word_invalid;
    record.channel_estimate_valid = rx_ctrl->rx_channel_estimate_info_vld;
    record.sig_len = rx_ctrl->sig_len;
    record.len = len;
    memcpy(record.csi, info->buf, len);

    atomic_fetch_add(&s_callback_records, 1);

    if (xQueueSend(s_queue, &record, 0) != pdTRUE) {
        atomic_fetch_add(&s_queue_drops, 1);
    }
}

/* ------------------------------------------------------ ARI2 output */

#if RX_OUTPUT_MODE == RX_OUTPUT_ARI2

#define ARI2_UART_PORT ((uart_port_t)CONFIG_ESP_CONSOLE_UART_NUM)

/* The driver requires an RX buffer larger than the hardware FIFO, although
 * nothing is read. */
#define ARI2_UART_RX_BUFFER_BYTES 256

/* Wait for the last boot log bytes to leave the UART FIFO. */
#define ARI2_LOG_DRAIN_MS 20

static int discard_log_output(const char *format, va_list arguments) {
    (void)format;
    (void)arguments;

    return 0;
}

/*
 * Switches the console UART to raw binary output.
 *
 * Until here, logs went through the console VFS, which turns "\n" into
 * "\r\n" (CONFIG_LIBC_STDOUT_LINE_ENDING_CRLF). Packets bypass it: they go
 * through the UART driver, which sends bytes unmodified. Any later text from
 * ESP_LOG (including the WiFi libraries) or esp_rom_printf would land
 * between or inside packets, so all of it is switched off. A panic still
 * prints; the PC resynchronises on the "ARI2" magic.
 */
static void ari2_output_start(void) {
    ESP_LOGI(
        TAG,
        "ari2 stream: room=%d node=%d bins=%d packet=%d bytes; logging off now",
        RX_ARI2_ROOM, RX_ARI2_NODE, ARI2_HT20_BINS, ARI2_HT20_PACKET_BYTES);
    fflush(stdout);

    /* The console writes above return once the bytes are in the 128-byte
     * hardware FIFO; give it time to drain (about 1.4 ms at 921600 baud). */
    vTaskDelay(pdMS_TO_TICKS(ARI2_LOG_DRAIN_MS));

    esp_log_level_set("*", ESP_LOG_NONE);
    esp_log_set_vprintf(discard_log_output);
    esp_rom_install_channel_putc(1, NULL);
    esp_rom_install_channel_putc(2, NULL);

    RX_CHECK(uart_driver_install(ARI2_UART_PORT, ARI2_UART_RX_BUFFER_BYTES,
                                 RX_ARI2_UART_TX_BUFFER_BYTES, 0, NULL, 0));
}

/* Returns false for frames that are not 57-pair HT20 HT-LTF CSI. */
static bool ari2_build_packet(const rx_record_t *record, uint32_t boot_id,
                              uint8_t packet[ARI2_HT20_PACKET_BYTES]) {
    if (RX_ARI2_REQUIRE_ESTIMATE_VALID && !record->channel_estimate_valid) {
        return false;
    }

    int8_t iq[ARI2_HT20_IQ_BYTES];
    bool mapped =
        ari2_map_ht20(record->csi, record->len, record->first_word_invalid != 0,
                      RX_ARI2_SOURCE_ORDER, iq);

    if (!mapped) {
        return false;
    }

    /* The TX payload counter serves as both sequence numbers: one CSI
     * sample per transmitted packet. */
    ari2_header_t header = {
        .room = RX_ARI2_ROOM,
        .node = RX_ARI2_NODE,
        .channel = record->channel,
        .flags = RX_ARI2_FLAGS,
        .boot_id = boot_id,
        .packet_seq = record->seq,
        .sample_seq = record->seq,
        .timestamp_us = record->hardware_timestamp_us,
        .rssi = record->rssi,
        .agc_gain = record->agc_gain,
        .fft_gain = record->fft_gain,
        .tx_power_quarter_dbm = RX_ARI2_TX_POWER_QUARTER_DBM,
    };

    ari2_pack_ht20(&header, iq, packet);

    return true;
}

static void processing_task(void *argument) {
    (void)argument;

    static rx_record_t record;
    static uint8_t packet[ARI2_HT20_PACKET_BYTES];

    /* Random per boot (the RF is on, so esp_random is a true RNG); lets the
     * PC tell a reboot from a sequence jump. */
    const uint32_t boot_id = esp_random();

    for (;;) {
        if (xQueueReceive(s_queue, &record, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        if (!ari2_build_packet(&record, boot_id, packet)) {
            continue;
        }

        /* One call per packet: the driver holds its TX lock for the whole
         * buffer, so a packet is never split by another writer. */
        uart_write_bytes(ARI2_UART_PORT, packet, sizeof(packet));

#if RX_ARI2_SEND_TO_MAIN
        /* esp_now_send copies the payload, so the buffer can be reused. A
         * failed send is not retried: the PC sees the gap in packet_seq. */
#if RX_ARI2_SEND_BROADCAST
        esp_now_send(s_broadcast_mac, packet, sizeof(packet));
#else
        esp_now_send(s_main_mac, packet, sizeof(packet));
#endif
#endif
    }
}

#endif /* RX_OUTPUT_MODE == RX_OUTPUT_ARI2 */

/* ------------------------------------------------------- text output */

#if RX_OUTPUT_MODE == RX_OUTPUT_TEXT

#if RX_RAW_CSI_OUTPUT

static const char BASE64_ALPHABET[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* `out` must hold 4 * ceil(n / 3) + 1 characters. */
static void base64_encode(const int8_t *in, size_t n, char *out) {
    size_t in_index = 0;
    size_t out_index = 0;

    while (in_index + 3 <= n) {
        uint32_t value = ((uint32_t)(uint8_t)in[in_index] << 16) |
                         ((uint32_t)(uint8_t)in[in_index + 1] << 8) |
                         (uint32_t)(uint8_t)in[in_index + 2];

        out[out_index++] = BASE64_ALPHABET[(value >> 18) & 0x3f];
        out[out_index++] = BASE64_ALPHABET[(value >> 12) & 0x3f];
        out[out_index++] = BASE64_ALPHABET[(value >> 6) & 0x3f];
        out[out_index++] = BASE64_ALPHABET[value & 0x3f];
        in_index += 3;
    }

    if (in_index < n) {
        uint32_t value = (uint32_t)(uint8_t)in[in_index] << 16;
        bool two_bytes = in_index + 1 < n;

        if (two_bytes) {
            value |= (uint32_t)(uint8_t)in[in_index + 1] << 8;
        }

        out[out_index++] = BASE64_ALPHABET[(value >> 18) & 0x3f];
        out[out_index++] = BASE64_ALPHABET[(value >> 12) & 0x3f];
        out[out_index++] =
            two_bytes ? BASE64_ALPHABET[(value >> 6) & 0x3f] : '=';
        out[out_index++] = '=';
    }

    out[out_index] = '\0';
}

/* Same field order as the collector format of the recorded sessions. */
static void print_raw_record(const rx_record_t *record, uint32_t drops) {
    static char encoded[4 * ((RX_CSI_MAX_BYTES + 2) / 3) + 1];

    base64_encode(record->csi, record->len, encoded);

    printf("CSI_DATA,%" PRIu32 ",%d,%d,%d,%u,%u,%" PRIu32
           ",%u,%u,%u,%u,%.6f,%" PRIu32 ",%s\n",
           record->seq, record->rssi, record->noise_floor, record->fft_gain,
           record->agc_gain, record->channel, record->hardware_timestamp_us,
           record->sig_len, record->rx_format, record->len,
           record->first_word_invalid, (double)record->compensate_gain, drops,
           encoded);
}

#endif /* RX_RAW_CSI_OUTPUT */

#if RX_REPORT_TO_MAIN

static uint32_t s_report_errors;

/* The payload is the line text without a terminator. A failed send is only
 * counted: the main unit sees the gap in the window numbers. */
static void report_to_main(const char *line) {
    size_t length = strlen(line);

    if (length == 0 || length > ESP_NOW_MAX_DATA_LEN) {
        s_report_errors++;
        return;
    }

    if (esp_now_send(s_main_mac, (const uint8_t *)line, length) != ESP_OK) {
        s_report_errors++;
    }
}

#endif /* RX_REPORT_TO_MAIN */

#if RX_RUN_OCCUPANCY_CORE

static void emit_line(const char *line) {
    puts(line);

#if RX_REPORT_TO_MAIN
    report_to_main(line);
#endif
}

static void print_output(const occ_output_t *output) {
    char line[256];

    if (output->has_stats) {
        occ_format_stats(line, sizeof(line), &output->stats);
        emit_line(line);
    }

    if (output->has_event) {
        occ_format_event(line, sizeof(line), &output->event);
        emit_line(line);
    }
}

#endif /* RX_RUN_OCCUPANCY_CORE */

/* -------------------------------------------------------- receive clock */

/* The core runs on the receive clock: rx_ctrl.timestamp extended to 64 bits
 * exactly as host/replay.c does with the raw lines. While idle, the clock is
 * estimated from esp_timer elapsed since the last packet. */
typedef struct {
    bool started;
    uint32_t previous;
    int64_t wraps;
    int64_t last_rx_us;
    int64_t last_queued_at_us;
} rx_clock_t;

static int64_t rx_clock_update(rx_clock_t *clock, const rx_record_t *record) {
    uint32_t timestamp = record->hardware_timestamp_us;

    if (clock->started && timestamp < clock->previous &&
        clock->previous - timestamp > 0x80000000u) {
        clock->wraps++;
    }

    clock->started = true;
    clock->previous = timestamp;
    clock->last_rx_us = clock->wraps * 4294967296LL + (int64_t)timestamp;
    clock->last_queued_at_us = record->queued_at_us;

    return clock->last_rx_us;
}

#if RX_RUN_OCCUPANCY_CORE

/* Conservative estimate: elapsed time since the last packet, minus a margin.
 * Never earlier than the last record (the core ignores such times anyway). */
static int64_t rx_clock_now(const rx_clock_t *clock) {
    int64_t elapsed_us = esp_timer_get_time() - clock->last_queued_at_us -
                         RX_IDLE_CLOCK_MARGIN_US;

    if (elapsed_us < 0) {
        elapsed_us = 0;
    }

    return clock->last_rx_us + elapsed_us;
}

/* --------------------------------------------------------- processing */

static void advance_until_quiet(occ_core_t *core, int64_t now_us) {
    occ_output_t output;

    do {
        occ_core_advance_time(core, now_us, &output);
        print_output(&output);
    } while (output.has_stats || output.has_event);
}

/* Feeds one record to the core in the PC replay order (advance_time, push,
 * poll). Returns false when the core rejects it. */
static bool run_core(occ_core_t *core, const rx_record_t *record,
                     int64_t rx_time_us) {
    advance_until_quiet(core, rx_time_us);

    occ_csi_record_t core_record = {
        .seq = record->seq,
        .rx_time_us = rx_time_us,
        .rssi = record->rssi,
        .agc_gain = record->agc_gain,
        .fft_gain = record->fft_gain,
        .csi_len = record->len,
        .csi = record->csi,
    };
    occ_output_t output;

    if (!occ_core_push(core, &core_record, &output)) {
        return false;
    }

    print_output(&output);

    while (occ_core_poll(core, &output)) {
        print_output(&output);
    }

    return true;
}

#endif /* RX_RUN_OCCUPANCY_CORE */

static void print_health(const rx_clock_t *clock, uint32_t processed,
                         uint32_t rejected) {
    int64_t last_packet_age_ms = -1;

    if (clock->started) {
        last_packet_age_ms =
            (esp_timer_get_time() - clock->last_queued_at_us) / 1000;
    }

    printf("OCC_HEALTH,%d,%lld,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32
           ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%u,%lld\n",
           RX_LINK_ID, (long long)(esp_timer_get_time() / 1000),
           (uint32_t)esp_get_free_heap_size(),
           (uint32_t)esp_get_minimum_free_heap_size(),
           (uint32_t)uxTaskGetStackHighWaterMark(s_processing_task),
           (uint32_t)atomic_load(&s_callback_records), processed, rejected,
           (uint32_t)atomic_load(&s_queue_drops),
           (unsigned)uxQueueMessagesWaiting(s_queue),
           (long long)last_packet_age_ms);
}

/* One OCC_GAIN line once the baseline is known:
 *   OCC_GAIN,link,force_requested,baseline_agc,baseline_fft,baseline_err,
 *   force_err,gain_status (0 collect, 1 ready, 2 forced) */
static void print_gain_report_once(void) {
    static bool printed;

    if (printed || !atomic_load(&s_gain_report_ready)) {
        return;
    }

    printf("OCC_GAIN,%d,%d,%u,%d,%s,%s,%d\n", RX_LINK_ID, RX_FORCE_RX_GAIN,
           (unsigned)s_baseline_agc, (int)s_baseline_fft,
           esp_err_to_name(s_baseline_err), esp_err_to_name(s_force_err),
           (int)esp_csi_gain_ctrl_get_gain_status());
    printed = true;
}

static void processing_task(void *argument) {
    (void)argument;

    static rx_record_t record;

#if RX_RUN_OCCUPANCY_CORE
    /* Static: the core keeps fixed per-subcarrier sums (about 6 KB). */
    static occ_core_t core;

    occ_config_t config;
    occ_default_config(&config);
    config.link_id = RX_LINK_ID;
    config.gain_mode = RX_CORE_GAIN_MODE;

    if (!occ_core_init(&core, &config)) {
        ESP_LOGE(TAG, "occ_core_init failed");
        abort();
    }
#endif

    rx_clock_t clock = {0};
    uint32_t processed = 0;
    uint32_t rejected = 0;
    int64_t next_health_us =
        esp_timer_get_time() + RX_HEALTH_INTERVAL_S * 1000000LL;

#if RX_RAW_CSI_OUTPUT
    printf("type,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_"
           "timestamp,sig_len,"
           "rx_format,len,first_word,compensate_gain,dropped,data\n");
#endif

    for (;;) {
        if (xQueueReceive(s_queue, &record, pdMS_TO_TICKS(RX_IDLE_CHECK_MS)) ==
            pdTRUE) {
#if RX_RAW_CSI_OUTPUT
            print_raw_record(&record, (uint32_t)atomic_load(&s_queue_drops));
#endif

            int64_t rx_time_us = rx_clock_update(&clock, &record);

#if RX_RUN_OCCUPANCY_CORE
            if (!run_core(&core, &record, rx_time_us)) {
                rejected++;
            }
#else
            (void)rx_time_us;
#endif

            processed++;
        }
#if RX_RUN_OCCUPANCY_CORE
        else if (clock.started) {
            advance_until_quiet(&core, rx_clock_now(&clock));
        }
#endif

        print_gain_report_once();

        if (esp_timer_get_time() >= next_health_us) {
            print_health(&clock, processed, rejected);
            next_health_us += RX_HEALTH_INTERVAL_S * 1000000LL;

#if RX_REPORT_TO_MAIN
            if (s_report_errors > 0) {
                ESP_LOGW(TAG, "report_to_main errors=%" PRIu32,
                         s_report_errors);
            }
#endif
        }
    }
}

#endif /* RX_OUTPUT_MODE == RX_OUTPUT_TEXT */

/* ------------------------------------------------------------ WiFi setup */

static void nvs_init(void) {
    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        RX_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }

    RX_CHECK(err);
}

/*
 * Call order (IDF 6.0.3 header notes): protocols and bandwidths need only an
 * initialised WiFi, band mode and channel need a started one. They are all
 * set after esp_wifi_start, in the order the previous receiver used: with the
 * protocol/bandwidth calls before start, the 5 GHz switch left the interface
 * without HT40 and esp_now_set_peer_rate_config failed with "need change
 * bandwidth to 40" (first flash, 2026-10-04).
 */
static void wifi_init(void) {
    RX_CHECK(esp_netif_init());
    RX_CHECK(esp_event_loop_create_default());

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    RX_CHECK(esp_wifi_init(&init_config));

    RX_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    RX_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    RX_CHECK(esp_wifi_start());

    RX_CHECK(esp_wifi_set_band_mode(RX_WIFI_BAND_MODE));

    wifi_protocols_t protocols = {
        .ghz_2g = RX_WIFI_PROTOCOL_2G,
        .ghz_5g = RX_WIFI_PROTOCOL_5G,
    };
    RX_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = RX_WIFI_BANDWIDTH_2G,
        .ghz_5g = RX_WIFI_BANDWIDTH_5G,
    };
    RX_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));

    RX_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    RX_CHECK(esp_wifi_set_channel(RX_WIFI_CHANNEL, RX_WIFI_SECOND_CHANNEL));
}

/* The previous receiver also initialised ESP-NOW with a broadcast peer; kept
 * so the radio state matches the recorded sessions. */
static void espnow_init(void) {
    RX_CHECK(esp_now_init());
    RX_CHECK(esp_now_set_pmk((const uint8_t *)RX_ESPNOW_PMK));

    esp_now_peer_info_t peer = {
        .channel = RX_WIFI_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    RX_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate_config = {
        .phymode = RX_ESPNOW_PHYMODE,
        .rate = RX_ESPNOW_RATE,
        .ersu = false,
        .dcm = false,
    };
    RX_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

#if RX_REPORT_TO_MAIN || RX_ARI2_SEND_TO_MAIN
    esp_now_peer_info_t main_peer = {
        .channel = RX_WIFI_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(main_peer.peer_addr, s_main_mac, ESP_NOW_ETH_ALEN);
    RX_CHECK(esp_now_add_peer(&main_peer));
    RX_CHECK(esp_now_set_peer_rate_config(main_peer.peer_addr, &rate_config));
#endif
}

static void csi_init(void) {
    s_queue = xQueueCreate(RX_QUEUE_LENGTH, sizeof(rx_record_t));

    if (s_queue == NULL) {
        ESP_LOGE(TAG, "xQueueCreate failed");
        abort();
    }

    BaseType_t created =
        xTaskCreate(processing_task, "occ_proc", RX_TASK_STACK_BYTES, NULL,
                    RX_TASK_PRIORITY, &s_processing_task);

    if (created != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreate(occ_proc) failed");
        abort();
    }

    RX_CHECK(esp_wifi_set_promiscuous(true));

    wifi_csi_config_t csi_config = {
        .enable = true,
        .acquire_csi_legacy = false,
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

    RX_CHECK(esp_wifi_set_csi_config(&csi_config));
    RX_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    RX_CHECK(esp_wifi_set_csi(true));
}

void app_main(void) {
    nvs_init();
    wifi_init();
    espnow_init();

    uint8_t own_mac[ESP_NOW_ETH_ALEN];
    RX_CHECK(esp_wifi_get_mac(WIFI_IF_STA, own_mac));

    uint8_t primary_channel = 0;
    wifi_second_chan_t second_channel = WIFI_SECOND_CHAN_NONE;
    RX_CHECK(esp_wifi_get_channel(&primary_channel, &second_channel));

    wifi_bandwidths_t bandwidths = {0};
    RX_CHECK(esp_wifi_get_bandwidths(WIFI_IF_STA, &bandwidths));

    ESP_LOGI(TAG, "================ CSI RECV ================");
    ESP_LOGI(TAG,
             "rx_config,mac=" MACSTR ",transmitter=" MACSTR
             ",link=%d,raw=%d,force_gain=%d,core_gain=%d",
             MAC2STR(own_mac), MAC2STR(s_transmitter_mac), RX_LINK_ID,
             RX_RAW_CSI_OUTPUT, RX_FORCE_RX_GAIN, (int)RX_CORE_GAIN_MODE);
    ESP_LOGI(TAG, "rx_config,channel=%u,second=%d,bw5g=%d", primary_channel,
             (int)second_channel, (int)bandwidths.ghz_5g);

#if RX_REPORT_TO_MAIN
    ESP_LOGI(TAG, "rx_config,report_to_main=" MACSTR, MAC2STR(s_main_mac));
#endif
    ESP_LOGI(TAG, "rx_config,output=%s,core=%d,bandwidth_mhz=%d",
             RX_OUTPUT_MODE == RX_OUTPUT_ARI2 ? "ari2" : "text",
             RX_RUN_OCCUPANCY_CORE, LINK_WIFI_BANDWIDTH_MHZ);
    ESP_LOGI(TAG, "occ_core_t %u bytes, rx_record_t %u bytes, queue %d",
             (unsigned)sizeof(occ_core_t), (unsigned)sizeof(rx_record_t),
             RX_QUEUE_LENGTH);

#if RX_OUTPUT_MODE == RX_OUTPUT_ARI2
    /* Last log line; from here on the console carries only ARI2 packets. */
    ari2_output_start();
#endif

    csi_init();
}
