/*
 * Transmitter settings, all in one place.
 *
 * The radio settings matched the transmitter that recorded the existing
 * sessions in ~/data/wifi-csi (git history before commit a577689,
 * csi-tx/main/csi-tx.c) until the channel width became a link setting
 * (LINK_WIFI_BANDWIDTH_MHZ, link_config.h). With 40 they are the recorded
 * HT40 setup again; with 20 (current) new captures are HT20 (report 0014).
 */
#pragma once

#include "esp_now.h"
#include "esp_wifi.h"

#include "link_config.h"

/* 5 GHz channel of this link (link_config.h). */
#define TX_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY
#define TX_WIFI_CHANNEL LINK_WIFI_CHANNEL
#define TX_WIFI_PROTOCOL_2G WIFI_PROTOCOL_11N
#define TX_WIFI_PROTOCOL_5G WIFI_PROTOCOL_11N

/* Channel width and ESP-NOW frame format follow LINK_WIFI_BANDWIDTH_MHZ:
 * HT20 or HT40 frames, MCS0, long guard interval. The receiver uses the
 * same switch (rx_config.h), so both ends always agree. */
#if LINK_WIFI_BANDWIDTH_MHZ == 20
#define TX_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_NONE
#define TX_WIFI_BANDWIDTH_2G WIFI_BW20
#define TX_WIFI_BANDWIDTH_5G WIFI_BW20
#define TX_ESPNOW_PHYMODE WIFI_PHY_MODE_HT20
#else
#define TX_WIFI_SECOND_CHANNEL WIFI_SECOND_CHAN_ABOVE
#define TX_WIFI_BANDWIDTH_2G WIFI_BW40
#define TX_WIFI_BANDWIDTH_5G WIFI_BW40
#define TX_ESPNOW_PHYMODE WIFI_PHY_MODE_HT40
#endif

#define TX_ESPNOW_RATE WIFI_PHY_RATE_MCS0_LGI
#define TX_ESPNOW_PMK "pmk1234567890123"

/* Send period. 10 ms needs CONFIG_FREERTOS_HZ=1000 (sdkconfig.defaults) so the
 * period is 10 ticks rather than 1 tick. */
#define TX_SEND_PERIOD_MS 10

/* Transmitter MAC of this link (link_config.h). The receiver filters CSI by
 * this address. */
#define TX_SOURCE_MAC LINK_TX_SOURCE_MAC

/* Receiver board MAC (base MAC of the link's RX ESP32-C5). */
#ifndef LINK_RECEIVER_MAC
#error "link_config.h has no LINK_RECEIVER_MAC for this CSI_LINK_ID yet"
#endif
#define TX_RECEIVER_MAC LINK_RECEIVER_MAC

/* Maximum TX power in 0.25 dBm units, range 8..84 (2..20 dBm); the driver
 * rounds down to its steps (esp_wifi.h attention 5). 0 keeps the driver
 * default, which is what the existing sessions were recorded with. Lower
 * power is being tested to reduce the direct TX->RX coupling inside the case
 * so reflections from the room take a larger share of the CSI range. */
#define TX_MAX_TX_POWER_QUARTER_DBM 8

/* How often the send statistics are logged. */
#define TX_REPORT_INTERVAL_S 10
