/*
 * Main unit: one board outside the rooms.
 *
 * It does three things and makes no occupancy decision itself:
 *
 * 1. It receives the report lines (OCC_STATS, OCC_EVENT) that the room
 *    receivers send by ESP-NOW and prints them unchanged. Each line carries
 *    its link ID.
 * 2. It listens to the room transmitters and prints the raw CSI of one link
 *    at a time, as CSI_LINK lines, for breathing detection downstream. The
 *    link is the one whose receiver last reported a run of motion seconds
 *    (MAIN_LISTEN_SWITCH_MOTION_SECONDS), unless the host has fixed it: a
 *    digit on the console selects that link, 'a' returns to automatic.
 * 3. It computes the 1 s motion statistic of every link on its own path
 *    (room transmitter -> main unit) and prints it as MAIN_STATS. The
 *    transmitter and the main unit are far apart, so this path covers much
 *    more of the space than the co-located link in the case (2026-10-05:
 *    leaving home was invisible to the living-room receiver and plain here).
 *
 * Output, one message per line (docs/spec/0002-output-messages.md):
 *   OCC_STATS, OCC_EVENT   forwarded from the receivers
 *   CSI_LINK    raw record of the listened link (CSI_DATA fields after the link ID)
 *   MAIN_STATS  1 s motion statistic of one link on the main unit's path
 *               (OCC_STATS fields; eta and decision are those of the case
 *               link and are not valid for this path)
 *   MAIN_LISTEN the listened link changed
 *   MAIN_HEALTH heap, stack, queue and report counters
 *
 * Both callbacks run in the WiFi task and only copy into a queue; the output
 * task prints.
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
#include "main_config.h"
#include "occupancy_core.h"

static const char *TAG = "csi-main";

static const uint8_t s_tx_mac_prefix[ESP_NOW_ETH_ALEN - 1] = LINK_TX_SOURCE_MAC_PREFIX;

/* Packets used per link to estimate the gain baseline for compensate_gain. */
#define GAIN_BASELINE_PACKETS 100

/* OCC_STATS decision values (occupancy core). */
#define DECISION_MOTION 1

/* Logs the failing call by name and stops (setup calls only). */
#define MAIN_CHECK(call)                                                       \
    do {                                                                       \
        esp_err_t check_err = (call);                                          \
        if (check_err != ESP_OK) {                                             \
            ESP_LOGE(TAG, "%s failed: %s", #call, esp_err_to_name(check_err)); \
            abort();                                                           \
        }                                                                      \
    } while (0)

/* One received transmitter packet, as queued from the CSI callback. */
typedef struct {
    uint32_t seq;
    uint32_t hardware_timestamp_us; /**< rx_ctrl.timestamp, wraps at 2^32 */
    float compensate_gain;          /**< reported only, never applied */
    int8_t rssi;
    int8_t noise_floor;
    int8_t fft_gain;
    uint8_t agc_gain;
    uint8_t channel;
    uint8_t rx_format;
    uint8_t first_word_invalid;
    uint8_t link_id;
    uint16_t sig_len;
    uint16_t len;
    int8_t csi[MAIN_CSI_MAX_BYTES];
} csi_record_t;

/* One report line from a receiver, as queued from the ESP-NOW callback. */
typedef struct {
    char text[MAIN_REPORT_MAX_CHARS + 1];
} report_t;

static QueueHandle_t s_csi_queue;
static QueueHandle_t s_report_queue;
static TaskHandle_t s_output_task;

static atomic_int s_listen_link = MAIN_DEFAULT_LISTEN_LINK;

/* True while the host has fixed the listened link (output task only). */
static bool s_listen_fixed;
static atomic_uint_fast32_t s_csi_records;
static atomic_uint_fast32_t s_csi_queue_drops;
static atomic_uint_fast32_t s_reports_received;
static atomic_uint_fast32_t s_report_drops;

/* ------------------------------------------------------------ callbacks */

/* Link ID of a transmitter address, or -1 when it is not one of ours. */
static int link_of_transmitter(const uint8_t *mac) {
    if (memcmp(mac, s_tx_mac_prefix, sizeof(s_tx_mac_prefix)) != 0) {
        return -1;
    }

    uint8_t link_id = mac[ESP_NOW_ETH_ALEN - 1];

    if (link_id >= LINK_MAX_LINKS) {
        return -1;
    }

    return link_id;
}

static void csi_callback(void *context, wifi_csi_info_t *info) {
    (void)context;

    if (info == NULL || info->buf == NULL) {
        return;
    }

    int link_id = link_of_transmitter(info->mac);

    if (link_id < 0) {
        return;
    }

    if (info->payload == NULL || info->payload_len < MAIN_SEQ_PAYLOAD_OFFSET + sizeof(uint32_t)) {
        return;
    }

    /* Static: too large for the WiFi task stack, and only this callback uses it. */
    static csi_record_t record;
    static uint32_t packet_count;
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    float compensate_gain = 1.0f;

    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);

    /* The baseline is taken once, from the first link listened to. It only
     * feeds the compensate_gain field, which nothing downstream applies. */
    if (packet_count < GAIN_BASELINE_PACKETS) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
        packet_count++;
    } else {
        esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
    }

    uint16_t len = info->len;

    if (len > MAIN_CSI_MAX_BYTES) {
        len = MAIN_CSI_MAX_BYTES;
    }

    memcpy(&record.seq, info->payload + MAIN_SEQ_PAYLOAD_OFFSET, sizeof(record.seq));
    record.hardware_timestamp_us = rx_ctrl->timestamp;
    record.compensate_gain = compensate_gain;
    record.rssi = rx_ctrl->rssi;
    record.noise_floor = rx_ctrl->noise_floor;
    record.fft_gain = fft_gain;
    record.agc_gain = agc_gain;
    record.channel = rx_ctrl->channel;
    record.rx_format = rx_ctrl->cur_bb_format;
    record.first_word_invalid = info->first_word_invalid;
    record.link_id = (uint8_t)link_id;
    record.sig_len = rx_ctrl->sig_len;
    record.len = len;
    memcpy(record.csi, info->buf, len);

    atomic_fetch_add(&s_csi_records, 1);

    if (xQueueSend(s_csi_queue, &record, 0) != pdTRUE) {
        atomic_fetch_add(&s_csi_queue_drops, 1);
    }
}

static void report_callback(const esp_now_recv_info_t *info, const uint8_t *data, int data_len) {
    (void)info;

    if (data == NULL || data_len <= 0 || data_len > MAIN_REPORT_MAX_CHARS) {
        return;
    }

    /* Static: only this callback uses it. */
    static report_t report;

    memcpy(report.text, data, (size_t)data_len);
    report.text[data_len] = '\0';

    /* Anything that is not a receiver report line is not ours. */
    if (strncmp(report.text, "OCC_", 4) != 0) {
        return;
    }

    atomic_fetch_add(&s_reports_received, 1);

    if (xQueueSend(s_report_queue, &report, 0) != pdTRUE) {
        atomic_fetch_add(&s_report_drops, 1);
    }
}

/* ------------------------------------------------------- output helpers */

static const char BASE64_ALPHABET[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* `out` must hold 4 * ceil(n / 3) + 1 characters. */
static void base64_encode(const int8_t *in, size_t n, char *out) {
    size_t in_index = 0;
    size_t out_index = 0;

    while (in_index + 3 <= n) {
        uint32_t value = ((uint32_t)(uint8_t)in[in_index] << 16)
                         | ((uint32_t)(uint8_t)in[in_index + 1] << 8)
                         | (uint32_t)(uint8_t)in[in_index + 2];

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
        out[out_index++] = two_bytes ? BASE64_ALPHABET[(value >> 6) & 0x3f] : '=';
        out[out_index++] = '=';
    }

    out[out_index] = '\0';
}

/* The CSI_DATA fields of the receiver, in the same order, after the link ID. */
static void print_csi_record(const csi_record_t *record, uint32_t drops) {
    static char encoded[4 * ((MAIN_CSI_MAX_BYTES + 2) / 3) + 1];

    base64_encode(record->csi, record->len, encoded);

    printf("CSI_LINK,%u,%" PRIu32 ",%d,%d,%d,%u,%u,%" PRIu32 ",%u,%u,%u,%u,%.6f,%" PRIu32 ",%s\n",
           record->link_id, record->seq, record->rssi, record->noise_floor, record->fft_gain,
           record->agc_gain, record->channel, record->hardware_timestamp_us, record->sig_len,
           record->rx_format, record->len, record->first_word_invalid,
           (double)record->compensate_gain, drops, encoded);
}

static void print_health(void) {
    printf("MAIN_HEALTH,%lld,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%d,%" PRIu32 ",%" PRIu32
           ",%" PRIu32 ",%" PRIu32 "\n",
           (long long)(esp_timer_get_time() / 1000),
           (uint32_t)esp_get_free_heap_size(),
           (uint32_t)esp_get_minimum_free_heap_size(),
           (uint32_t)uxTaskGetStackHighWaterMark(s_output_task),
           atomic_load(&s_listen_link),
           (uint32_t)atomic_load(&s_csi_records),
           (uint32_t)atomic_load(&s_csi_queue_drops),
           (uint32_t)atomic_load(&s_reports_received),
           (uint32_t)atomic_load(&s_report_drops));
}

/* ---------------------------------------------------- main-path motion */

/* Per link: a motion-statistic core and the 64-bit extension of the 32-bit
 * hardware receive time, as in csi-rx.c. */
typedef struct {
    occ_core_t core;
    bool clock_started;
    uint32_t previous_timestamp;
    int64_t wraps;
} link_path_t;

static int64_t extend_timestamp(link_path_t *path, uint32_t timestamp) {
    if (path->clock_started && timestamp < path->previous_timestamp
        && path->previous_timestamp - timestamp > 0x80000000u) {
        path->wraps++;
    }

    path->clock_started = true;
    path->previous_timestamp = timestamp;

    return path->wraps * 4294967296LL + (int64_t)timestamp;
}

/* Only the statistics are used; the core's occupancy events are ignored. */
static void print_path_stats(const occ_output_t *output) {
    char line[256];

    if (!output->has_stats) {
        return;
    }

    occ_format_stats(line, sizeof(line), &output->stats);

    /* "OCC_STATS,..." -> "MAIN_STATS,...": same fields, different source. */
    printf("MAIN%s\n", line + 3);
}

static void update_path_motion(link_path_t *path, const csi_record_t *record) {
    occ_csi_record_t core_record = {
        .seq = record->seq,
        .rx_time_us = extend_timestamp(path, record->hardware_timestamp_us),
        .rssi = record->rssi,
        .agc_gain = record->agc_gain,
        .fft_gain = record->fft_gain,
        .csi_len = record->len,
        .csi = record->csi,
    };
    occ_output_t output;

    if (!occ_core_push(&path->core, &core_record, &output)) {
        return;
    }

    print_path_stats(&output);

    while (occ_core_poll(&path->core, &output)) {
        print_path_stats(&output);
    }
}

/* ------------------------------------------------------ listen selection */

/* Run of consecutive motion seconds per link, from the forwarded OCC_STATS. */
typedef struct {
    uint32_t last_window;
    int motion_run;
} link_motion_t;

/* Commas in a complete OCC_STATS line (14 fields). */
#define OCC_STATS_COMMAS 13

/* Parses a whole decimal field that ends at `terminator`; false otherwise. */
static bool parse_field(const char *text, char terminator, long *value) {
    char *end = NULL;

    *value = strtol(text, &end, 10);

    return end != text && *end == terminator;
}

/*
 * Reads link, window and decision out of an OCC_STATS line:
 *   OCC_STATS,link,window,first_us,last_us,motion,packets,pairs,lost,
 *   duplicates,subcarriers,status,eta,decision
 * A line that was cut short or is otherwise malformed is rejected, so it
 * cannot count as a motion report.
 */
static bool parse_stats(const char *line, int *link_id, uint32_t *window, int *decision) {
    static const char prefix[] = "OCC_STATS,";

    if (strncmp(line, prefix, sizeof(prefix) - 1) != 0) {
        return false;
    }

    int commas = 0;

    for (const char *cursor = line; *cursor != '\0'; cursor++) {
        if (*cursor == ',') {
            commas++;
        }
    }

    if (commas != OCC_STATS_COMMAS) {
        return false;
    }

    const char *link_text = line + sizeof(prefix) - 1;
    const char *window_text = strchr(link_text, ',') + 1;
    const char *decision_text = strrchr(line, ',') + 1;

    long parsed_link = 0;
    long parsed_window = 0;
    long parsed_decision = 0;

    if (!parse_field(link_text, ',', &parsed_link) || !parse_field(window_text, ',', &parsed_window)
        || !parse_field(decision_text, '\0', &parsed_decision)) {
        return false;
    }

    if (parsed_link < 0 || parsed_link >= LINK_MAX_LINKS || parsed_window < 0) {
        return false;
    }

    *link_id = (int)parsed_link;
    *window = (uint32_t)parsed_window;
    *decision = (int)parsed_decision;

    return true;
}

/* MAIN_LISTEN,link,uptime_ms,fixed (1 while the host has fixed the link) */
static void print_listen_link(void) {
    printf("MAIN_LISTEN,%d,%lld,%d\n", atomic_load(&s_listen_link),
           (long long)(esp_timer_get_time() / 1000), s_listen_fixed ? 1 : 0);
}

static void set_listen_link(int link_id) {
    if (atomic_load(&s_listen_link) == link_id) {
        return;
    }

    atomic_store(&s_listen_link, link_id);
    print_listen_link();
}

static void update_listen_link(link_motion_t *links, const char *line) {
    int link_id = 0;
    uint32_t window = 0;
    int decision = 0;

    if (!parse_stats(line, &link_id, &window, &decision)) {
        return;
    }

    link_motion_t *link = &links[link_id];
    bool consecutive = window == link->last_window + 1;

    if (decision != DECISION_MOTION) {
        link->motion_run = 0;
    } else if (consecutive) {
        link->motion_run++;
    } else {
        link->motion_run = 1;
    }

    link->last_window = window;

    if (s_listen_fixed || link->motion_run < MAIN_LISTEN_SWITCH_MOTION_SECONDS) {
        return;
    }

    set_listen_link(link_id);
}

/*
 * Host commands, one character each on the console:
 *   '0'..     listen to that link (below LINK_MAX_LINKS) until told otherwise
 *   'a'       back to automatic selection
 * Anything else is ignored. The console read does not block: without the
 * UART driver installed it returns EOF when no character is waiting.
 */
static void poll_host_command(void) {
    int character = fgetc(stdin);

    if (character == EOF) {
        clearerr(stdin);
        return;
    }

    if (character == 'a') {
        s_listen_fixed = false;
        print_listen_link();
        return;
    }

    int link_id = character - '0';

    if (link_id < 0 || link_id >= LINK_MAX_LINKS) {
        return;
    }

    s_listen_fixed = true;

    if (atomic_load(&s_listen_link) == link_id) {
        print_listen_link();
        return;
    }

    set_listen_link(link_id);
}

/* ----------------------------------------------------------- output task */

static void output_task(void *argument) {
    (void)argument;

    static csi_record_t record;
    static report_t report;
    static link_motion_t links[LINK_MAX_LINKS];
    static link_path_t paths[LINK_MAX_LINKS];

    for (int link_id = 0; link_id < LINK_MAX_LINKS; link_id++) {
        occ_config_t config;
        occ_default_config(&config);
        config.link_id = link_id;
        config.gain_mode = OCC_GAIN_NORMALIZE;

        if (!occ_core_init(&paths[link_id].core, &config)) {
            ESP_LOGE(TAG, "occ_core_init failed");
            abort();
        }
    }

    int64_t next_health_us = esp_timer_get_time() + MAIN_HEALTH_INTERVAL_S * 1000000LL;

    print_listen_link();

    for (;;) {
        poll_host_command();

        /* Reports are few and small; drain them before waiting for CSI. */
        while (xQueueReceive(s_report_queue, &report, 0) == pdTRUE) {
            puts(report.text);
            update_listen_link(links, report.text);
        }

        if (xQueueReceive(s_csi_queue, &record, pdMS_TO_TICKS(MAIN_IDLE_CHECK_MS)) == pdTRUE) {
            if (record.link_id == atomic_load(&s_listen_link)) {
                print_csi_record(&record, (uint32_t)atomic_load(&s_csi_queue_drops));
            }

            update_path_motion(&paths[record.link_id], &record);
        }

        if (esp_timer_get_time() >= next_health_us) {
            print_health();
            next_health_us += MAIN_HEALTH_INTERVAL_S * 1000000LL;
        }
    }
}

/* ------------------------------------------------------- ARI2 relay */

#define ARI2_UART_PORT ((uart_port_t)CONFIG_ESP_CONSOLE_UART_NUM)

/* The driver requires an RX buffer larger than the hardware FIFO, although
 * nothing is read. */
#define ARI2_UART_RX_BUFFER_BYTES 256

/* Wait for the last boot log bytes to leave the UART FIFO. */
#define ARI2_LOG_DRAIN_MS 20

typedef struct {
    uint8_t bytes[MAIN_ARI2_PACKET_BYTES];
} ari2_packet_t;

static QueueHandle_t s_ari2_queue;
static atomic_uint_fast32_t s_ari2_drops;

/* Receiver packets only: exactly one ARI2 HT20 packet per ESP-NOW frame. */
static void ari2_relay_callback(const esp_now_recv_info_t *info, const uint8_t *data,
                                int data_len) {
    (void)info;

    if (data == NULL || data_len != MAIN_ARI2_PACKET_BYTES) {
        return;
    }

    if (memcmp(data, "ARI2", 4) != 0) {
        return;
    }

    /* Static: only this callback uses it. */
    static ari2_packet_t packet;

    memcpy(packet.bytes, data, MAIN_ARI2_PACKET_BYTES);

    if (xQueueSend(s_ari2_queue, &packet, 0) != pdTRUE) {
        atomic_fetch_add(&s_ari2_drops, 1);
    }
}

static int discard_log_output(const char *format, va_list arguments) {
    (void)format;
    (void)arguments;

    return 0;
}

/* Same switch as the receiver (csi-rx.c, ari2_output_start): from here on
 * the console UART carries only raw ARI2 packets, written through the UART
 * driver so no "\n" -> "\r\n" translation touches them. */
static void ari2_output_start(void) {
    ESP_LOGI(TAG, "ari2 relay: packet=%d bytes; logging off now", MAIN_ARI2_PACKET_BYTES);
    fflush(stdout);

    vTaskDelay(pdMS_TO_TICKS(ARI2_LOG_DRAIN_MS));

    esp_log_level_set("*", ESP_LOG_NONE);
    esp_log_set_vprintf(discard_log_output);
    esp_rom_install_channel_putc(1, NULL);
    esp_rom_install_channel_putc(2, NULL);

    MAIN_CHECK(uart_driver_install(ARI2_UART_PORT, ARI2_UART_RX_BUFFER_BYTES,
                                   MAIN_ARI2_UART_TX_BUFFER_BYTES, 0, NULL, 0));
}

static void ari2_relay_task(void *argument) {
    (void)argument;

    static ari2_packet_t packet;

    for (;;) {
        if (xQueueReceive(s_ari2_queue, &packet, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        /* One call per packet: the driver holds its TX lock for the whole
         * buffer, so a packet is never split. */
        uart_write_bytes(ARI2_UART_PORT, packet.bytes, sizeof(packet.bytes));
    }
}

/* Boot id written into the main-path packets (random per boot). */
static uint32_t s_main_boot_id;

/*
 * Main path: CSI of a room transmitter's frame as heard by the main unit,
 * packed as ARI2 right here (mapping and packing are a few hundred bytes of
 * copying) and queued for the same UART writer as the relayed packets.
 */
static void main_path_csi_callback(void *context, wifi_csi_info_t *info) {
    (void)context;

    if (info == NULL || info->buf == NULL) {
        return;
    }

    int link_id = link_of_transmitter(info->mac);

    if (link_id < 0) {
        return;
    }

    if (info->payload == NULL || info->payload_len < MAIN_SEQ_PAYLOAD_OFFSET + sizeof(uint32_t)) {
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    if (!rx_ctrl->rx_channel_estimate_info_vld) {
        return;
    }

    int8_t iq[ARI2_HT20_IQ_BYTES];
    bool mapped = ari2_map_ht20(info->buf, info->len, info->first_word_invalid != 0,
                                ARI2_SOURCE_ASCENDING, iq);

    if (!mapped) {
        return;
    }

    uint32_t seq = 0;
    memcpy(&seq, info->payload + MAIN_SEQ_PAYLOAD_OFFSET, sizeof(seq));

    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);

    ari2_header_t header = {
        .room = (uint8_t)(link_id + 1),
        .node = MAIN_ARI2_NODE,
        .channel = rx_ctrl->channel,
        .flags = 0,
        .boot_id = s_main_boot_id,
        .packet_seq = seq,
        .sample_seq = seq,
        .timestamp_us = rx_ctrl->timestamp,
        .rssi = rx_ctrl->rssi,
        .agc_gain = agc_gain,
        .fft_gain = fft_gain,
        .tx_power_quarter_dbm = 0,
    };

    /* Static: only this callback uses it. */
    static ari2_packet_t packet;

    ari2_pack_ht20(&header, iq, packet.bytes);

    if (xQueueSend(s_ari2_queue, &packet, 0) != pdTRUE) {
        atomic_fetch_add(&s_ari2_drops, 1);
    }
}

static void main_path_csi_init(void) {
    MAIN_CHECK(esp_wifi_set_promiscuous(true));

    wifi_csi_config_t csi_config = {
        .enable = true,
        .acquire_csi_legacy = false,
        .acquire_csi_force_lltf = false,
        .acquire_csi_ht20 = true,
        .acquire_csi_ht40 = false,
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

    MAIN_CHECK(esp_wifi_set_csi_config(&csi_config));
    MAIN_CHECK(esp_wifi_set_csi_rx_cb(main_path_csi_callback, NULL));
    MAIN_CHECK(esp_wifi_set_csi(true));
}

static void ari2_relay_init(void) {
    s_ari2_queue = xQueueCreate(MAIN_ARI2_QUEUE_LENGTH, sizeof(ari2_packet_t));

    if (s_ari2_queue == NULL) {
        ESP_LOGE(TAG, "xQueueCreate(ari2) failed");
        abort();
    }

    BaseType_t created = xTaskCreate(ari2_relay_task, "ari2_relay", MAIN_TASK_STACK_BYTES, NULL,
                                     MAIN_TASK_PRIORITY, NULL);

    if (created != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreate(ari2_relay) failed");
        abort();
    }

    ari2_output_start();

    MAIN_CHECK(esp_now_init());
    MAIN_CHECK(esp_now_set_pmk((const uint8_t *)MAIN_ESPNOW_PMK));
    MAIN_CHECK(esp_now_register_recv_cb(ari2_relay_callback));

    if (MAIN_ARI2_MAIN_PATH) {
        s_main_boot_id = esp_random();
        main_path_csi_init();
    }
}

/* ------------------------------------------------------------ WiFi setup */

static void nvs_init(void) {
    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        MAIN_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }

    MAIN_CHECK(err);
}

/* Same call order as the receiver (csi-rx.c, wifi_init): band, protocol and
 * bandwidth after esp_wifi_start, otherwise the interface is left without
 * HT40. */
static void wifi_init(void) {
    MAIN_CHECK(esp_netif_init());
    MAIN_CHECK(esp_event_loop_create_default());

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    MAIN_CHECK(esp_wifi_init(&init_config));

    MAIN_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    MAIN_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    MAIN_CHECK(esp_wifi_start());

    MAIN_CHECK(esp_wifi_set_band_mode(MAIN_WIFI_BAND_MODE));

    wifi_protocols_t protocols = {
        .ghz_2g = MAIN_WIFI_PROTOCOL_2G,
        .ghz_5g = MAIN_WIFI_PROTOCOL_5G,
    };
    MAIN_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = MAIN_WIFI_BANDWIDTH_2G,
        .ghz_5g = MAIN_WIFI_BANDWIDTH_5G,
    };
    MAIN_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));

    MAIN_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    MAIN_CHECK(esp_wifi_set_channel(MAIN_WIFI_CHANNEL, MAIN_WIFI_SECOND_CHANNEL));
}

static void espnow_init(void) {
    MAIN_CHECK(esp_now_init());
    MAIN_CHECK(esp_now_set_pmk((const uint8_t *)MAIN_ESPNOW_PMK));
    MAIN_CHECK(esp_now_register_recv_cb(report_callback));
}

static void csi_init(void) {
    MAIN_CHECK(esp_wifi_set_promiscuous(true));

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

    MAIN_CHECK(esp_wifi_set_csi_config(&csi_config));
    MAIN_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    MAIN_CHECK(esp_wifi_set_csi(true));
}

static void queues_and_task_init(void) {
    s_csi_queue = xQueueCreate(MAIN_CSI_QUEUE_LENGTH, sizeof(csi_record_t));
    s_report_queue = xQueueCreate(MAIN_REPORT_QUEUE_LENGTH, sizeof(report_t));

    if (s_csi_queue == NULL || s_report_queue == NULL) {
        ESP_LOGE(TAG, "xQueueCreate failed");
        abort();
    }

    BaseType_t created = xTaskCreate(output_task, "main_out", MAIN_TASK_STACK_BYTES, NULL,
                                     MAIN_TASK_PRIORITY, &s_output_task);

    if (created != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreate(main_out) failed");
        abort();
    }
}

void app_main(void) {
    nvs_init();
    wifi_init();

    uint8_t own_mac[ESP_NOW_ETH_ALEN];
    MAIN_CHECK(esp_wifi_get_mac(WIFI_IF_STA, own_mac));

    uint8_t primary_channel = 0;
    wifi_second_chan_t second_channel = WIFI_SECOND_CHAN_NONE;
    MAIN_CHECK(esp_wifi_get_channel(&primary_channel, &second_channel));

    wifi_bandwidths_t bandwidths = {0};
    MAIN_CHECK(esp_wifi_get_bandwidths(WIFI_IF_STA, &bandwidths));

    ESP_LOGI(TAG, "================ CSI MAIN ================");
    ESP_LOGI(TAG, "main_config,mac=" MACSTR ",links=%d,default_listen=%d", MAC2STR(own_mac),
             LINK_MAX_LINKS, MAIN_DEFAULT_LISTEN_LINK);
    ESP_LOGI(TAG, "main_config,channel=%u,second=%d,bw5g=%d", primary_channel, (int)second_channel,
             (int)bandwidths.ghz_5g);

    /* Plain if (not #if) keeps the text-mode functions compiled and used. */
    if (MAIN_OUTPUT_ARI2_RELAY) {
        ari2_relay_init();
        return;
    }

    /* The queues exist before either callback can fire. */
    queues_and_task_init();
    espnow_init();
    csi_init();
}
