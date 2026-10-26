/*
 * Main unit settings, all in one place. The radio settings are those of the
 * room links (csi-rx/main/rx_config.h); all links share one channel so the
 * main unit hears every transmitter without switching.
 */
#pragma once

#include "esp_now.h"
#include "esp_wifi.h"

#include "link_config.h"

#define MAIN_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY
/* The AP channel in AP receive mode (MAIN_ARI2_RX_AP): the case boards
 * (caselink ch) have to use the same one. */
#define MAIN_WIFI_CHANNEL 36
#define MAIN_WIFI_PROTOCOL_2G WIFI_PROTOCOL_11N
#define MAIN_WIFI_PROTOCOL_5G WIFI_PROTOCOL_11N

/* Same bandwidth as the room links (link_config.h). */
#if LINK_WIFI_BANDWIDTH_MHZ == 20
#define MAIN_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_NONE
#define MAIN_WIFI_BANDWIDTH_2G WIFI_BW20
#define MAIN_WIFI_BANDWIDTH_5G WIFI_BW20
#else
#define MAIN_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_ABOVE
#define MAIN_WIFI_BANDWIDTH_2G WIFI_BW40
#define MAIN_WIFI_BANDWIDTH_5G WIFI_BW40
#endif

/* 1: relay mode (report 0014). The main unit only forwards the ARI2 packets
 * the room receivers send it (ESP-NOW, 154 bytes each) to the PC as raw
 * bytes on the console UART; its own CSI capture and text lines are off.
 * 0: the previous text outputs (MAIN_STATS, CSI_LINK, ...). */
#define MAIN_OUTPUT_ARI2_RELAY (LINK_WIFI_BANDWIDTH_MHZ == 20)

/* 1 (relay mode): the main unit also captures the CSI of every room
 * transmitter it hears (the main path) and writes it as ARI2 packets with
 * node MAIN_ARI2_NODE and room = link + 1, next to the relayed packets.
 * Off when the main unit stands in for the main module (MAIN_ARI2_RX_AP),
 * which only takes the receivers' UDP packets and hears no transmitter. */
#define MAIN_ARI2_MAIN_PATH (!MAIN_ARI2_RX_AP)
#define MAIN_ARI2_NODE 2

/* 1 (relay mode): the main unit is the AP the case receivers join
 * (caselink relay=3) and takes their ARI2 packets as UDP datagrams on
 * MAIN_AP_UDP_PORT instead of ESP-NOW. SSID and password come from
 * caselink/main/ap_secret.h, the file the case receivers are built with.
 * The AP takes the default esp_netif address 192.168.4.1. */
#define MAIN_ARI2_RX_AP 0
#define MAIN_AP_UDP_PORT 3334
#define MAIN_AP_MAX_STATIONS 4

/* The interface the main unit runs on. */
#define MAIN_WIFI_IF (MAIN_OUTPUT_ARI2_RELAY && MAIN_ARI2_RX_AP ? WIFI_IF_AP : WIFI_IF_STA)

/* One ARI2 HT20 packet: 40-byte header + 57 IQ pairs. */
#define MAIN_ARI2_PACKET_BYTES 154

/* Callback -> UART task queue; about 0.3 s of two rooms on both paths. */
#define MAIN_ARI2_QUEUE_LENGTH 128

/* UART TX ring buffer of the relay writer (about 26 packets). */
#define MAIN_ARI2_UART_TX_BUFFER_BYTES 4096

#define MAIN_ESPNOW_PMK "pmk1234567890123"

/* Offset of the uint32 seq counter inside a transmitter frame payload (same
 * as RX_SEQ_PAYLOAD_OFFSET). */
#define MAIN_SEQ_PAYLOAD_OFFSET 15

/* Raw CSI is printed for one link at a time: two links at 100 Hz would not
 * fit the 921600 baud console. The main unit listens to the link whose
 * receiver last reported this many consecutive motion seconds, i.e. the room
 * the resident was last seen moving in, which is where breathing has to be
 * checked next. */
#define MAIN_LISTEN_SWITCH_MOTION_SECONDS 2

/* Link listened to until the first motion report arrives. */
#define MAIN_DEFAULT_LISTEN_LINK 0

/* Bytes of CSI kept per packet (HT40 rx_format 2 delivers 234). */
#define MAIN_CSI_MAX_BYTES 256

/* Callback -> output task queues. */
#define MAIN_CSI_QUEUE_LENGTH 32
#define MAIN_REPORT_QUEUE_LENGTH 16

/* Longest report line accepted from a receiver (OCC_STATS is about 110). */
#define MAIN_REPORT_MAX_CHARS 200

/* Output task. */
#define MAIN_TASK_STACK_BYTES 6144
#define MAIN_TASK_PRIORITY 4
#define MAIN_IDLE_CHECK_MS 100

/* Status line period. */
#define MAIN_HEALTH_INTERVAL_S 10
