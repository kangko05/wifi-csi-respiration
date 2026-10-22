/*
 * Receiver settings, all in one place. Radio settings match the
 * transmitter's tx_config.h. With LINK_WIFI_BANDWIDTH_MHZ 40 they are the
 * receiver that recorded the existing sessions (git history before a577689,
 * csi-rx/main/csi-rx.c); with 20 (current) the link is HT20 (report 0014).
 */
#pragma once

#include "esp_now.h"
#include "esp_wifi.h"

#include "ari2_packet.h"
#include "link_config.h"

#define RX_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY
#define RX_WIFI_CHANNEL LINK_WIFI_CHANNEL
#define RX_WIFI_PROTOCOL_2G WIFI_PROTOCOL_11N
#define RX_WIFI_PROTOCOL_5G WIFI_PROTOCOL_11N

#if LINK_WIFI_BANDWIDTH_MHZ == 20
#define RX_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_NONE
#define RX_WIFI_BANDWIDTH_2G WIFI_BW20
#define RX_WIFI_BANDWIDTH_5G WIFI_BW20
#define RX_ESPNOW_PHYMODE WIFI_PHY_MODE_HT20
#else
#define RX_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_ABOVE
#define RX_WIFI_BANDWIDTH_2G WIFI_BW40
#define RX_WIFI_BANDWIDTH_5G WIFI_BW40
#define RX_ESPNOW_PHYMODE WIFI_PHY_MODE_HT40
#endif

#define RX_ESPNOW_RATE WIFI_PHY_RATE_MCS0_LGI
#define RX_ESPNOW_PMK "pmk1234567890123"

/* Only CSI from this link's transmitter address is used (link_config.h). */
#define RX_TRANSMITTER_MAC LINK_TX_SOURCE_MAC

/* Offset of the uint32 seq counter inside the received frame payload, as
 * read by the receiver that recorded the existing sessions. */
#define RX_SEQ_PAYLOAD_OFFSET 15

/* Copied into every output message. Chosen at build time (link_config.h). */
#define RX_LINK_ID CSI_LINK_ID

/*
 * Serial output format (UART0, 921600 baud, the CP2102N USB bridge).
 *
 * RX_OUTPUT_ARI2: one binary ARI2 packet (ari2_packet.h, 154 bytes) per
 *     accepted CSI frame, for the PC breathing pipeline. Needs
 *     LINK_WIFI_BANDWIDTH_MHZ 20. Boot log lines come first; after that all
 *     logging is switched off so no text lands inside the byte stream. The
 *     onboard occupancy core does not run (report 0014: its subcarrier set
 *     and calibration are HT40-only, and its text lines would break the
 *     stream), so nothing is reported to the main unit either.
 * RX_OUTPUT_TEXT: the previous text lines (docs/spec/0002-output-messages.md):
 *     CSI_DATA, OCC_STATS, OCC_EVENT, OCC_HEALTH, OCC_GAIN. The occupancy
 *     core runs only with LINK_WIFI_BANDWIDTH_MHZ 40; at 20 MHz the text
 *     mode prints CSI_DATA (len 114) and OCC_HEALTH only.
 */
#define RX_OUTPUT_TEXT 0
#define RX_OUTPUT_ARI2 1

#ifndef RX_OUTPUT_MODE
#define RX_OUTPUT_MODE RX_OUTPUT_ARI2
#endif

#if RX_OUTPUT_MODE == RX_OUTPUT_ARI2

#if LINK_WIFI_BANDWIDTH_MHZ != 20
#error "RX_OUTPUT_ARI2 needs LINK_WIFI_BANDWIDTH_MHZ 20 (57 HT20 bins)"
#endif

#define RX_RUN_OCCUPANCY_CORE 0
#define RX_RAW_CSI_OUTPUT 0
#define RX_REPORT_TO_MAIN 0

/* 1: every ARI2 packet is also sent to the main unit (ESP-NOW unicast to
 * RX_MAIN_MAC, the 154 packet bytes as payload). The main unit writes it to
 * the PC, so the room case needs only power. The UART copy stays. */
#define RX_ARI2_SEND_TO_MAIN 1

#elif RX_OUTPUT_MODE == RX_OUTPUT_TEXT

#define RX_RUN_OCCUPANCY_CORE (LINK_WIFI_BANDWIDTH_MHZ == 40)

/* 1: also print every CSI record as a CSI_DATA line (collector format), for
 * replay on the PC. 0: decisions only. */
#define RX_RAW_CSI_OUTPUT 1

/* 1: also send every OCC_STATS and OCC_EVENT line to the main unit (ESP-NOW
 * unicast, the line text as payload). The lines are still printed. Only
 * meaningful while the occupancy core runs. */
#define RX_REPORT_TO_MAIN RX_RUN_OCCUPANCY_CORE

#define RX_ARI2_SEND_TO_MAIN 0

#else
#error "RX_OUTPUT_MODE must be RX_OUTPUT_TEXT or RX_OUTPUT_ARI2"
#endif

#define RX_MAIN_MAC LINK_MAIN_MAC

/* 1: the ARI2 copy for the main unit goes out as an ESP-NOW broadcast instead
 * of a unicast to RX_MAIN_MAC. No ACK is expected, so a receiver whose gain is
 * locked low (in-case baseline) still gets its packets out when it cannot hear
 * the main unit's ACKs; the main unit accepts any ARI2 frame. */
#ifndef RX_ARI2_SEND_BROADCAST
#define RX_ARI2_SEND_BROADCAST 0
#endif

/* ARI2 header values (RX_OUTPUT_ARI2). Room and node identify this stream
 * on the PC; node 1 is the receiver inside the room case. */
/* Room number = link + 1: link 0 (room case) is room 1, link 1 (living room) is room 2. */
#define RX_ARI2_ROOM (CSI_LINK_ID + 1)
#define RX_ARI2_NODE 1
#define RX_ARI2_FLAGS 0

/* The receiver cannot read the transmitter's power; 0 means unknown. If it
 * is set, use the transmitter's TX_MAX_TX_POWER_QUARTER_DBM. The PC uses it
 * only as part of a gain key, so any constant works. */
#define RX_ARI2_TX_POWER_QUARTER_DBM 0

/* Pair order of the HT20 CSI buffer (ari2_packet.h). ASCENDING follows this
 * repository's HT40 recordings; DC_FIRST follows the ESP-IDF table. Not yet
 * checked on HT20 hardware: on the PC, the all-zero bin must be bin 28
 * (k = 0). If it shows up at bin 0 instead, switch to DC_FIRST. */
#define RX_ARI2_SOURCE_ORDER ARI2_SOURCE_ASCENDING

/* 1: frames whose rx_ctrl.rx_channel_estimate_info_vld is 0 are dropped
 * (ESP-IDF: the CSI is then invalid). */
#define RX_ARI2_REQUIRE_ESTIMATE_VALID 1

/* UART TX ring buffer of the ARI2 writer: about 26 packets, so the
 * processing task does not wait for the wire (15.4 kB/s of 92 kB/s). */
#define RX_ARI2_UART_TX_BUFFER_BYTES 4096

/* Receive gain (ticket 08). After GAIN_BASELINE_PACKETS packets the median
 * AGC/FFT gain is the baseline; with RX_FORCE_RX_GAIN 1 the receiver then
 * fixes its gain to that baseline (esp_csi_gain_ctrl_set_rx_force_gain). */
#ifndef RX_FORCE_RX_GAIN
#define RX_FORCE_RX_GAIN 1
#endif

/* Gain handling in the decision core: OCC_GAIN_NORMALIZE or OCC_GAIN_NONE. */
#define RX_CORE_GAIN_MODE OCC_GAIN_NORMALIZE

/* Bytes of CSI kept per packet (HT40 HT-LTF delivers 234, HT20 HT-LTF 114). */
#define RX_CSI_MAX_BYTES 256

/* Callback -> processing task queue. */
#define RX_QUEUE_LENGTH 32

/* Processing task. */
#define RX_TASK_STACK_BYTES 6144
#define RX_TASK_PRIORITY 4
#define RX_IDLE_CHECK_MS 100

/* While idle the receive clock is estimated from esp_timer. The estimate is
 * held back by this margin so it never runs ahead of the hardware timestamp
 * of the next record (callback latency, clock drift); the device then never
 * acts on silence earlier than a PC replay of the same records would. */
#define RX_IDLE_CLOCK_MARGIN_US 100000

/* Status line period. */
#define RX_HEALTH_INTERVAL_S 10
