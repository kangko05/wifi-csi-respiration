#pragma once

#include "esp_wifi_types.h"

/* Wi-Fi settings: keep channel, bandwidth and protocol compatible with TX. */
#define CSI_RX_CHANNEL 36
#define CSI_RX_WIFI_BAND_MODE WIFI_BAND_MODE_5G_ONLY
#define CSI_RX_WIFI_2G_BANDWIDTH WIFI_BW40
#define CSI_RX_WIFI_5G_BANDWIDTH WIFI_BW40
#define CSI_RX_WIFI_2G_PROTOCOL WIFI_PROTOCOL_11N
#define CSI_RX_WIFI_5G_PROTOCOL WIFI_PROTOCOL_11N

/* ESP-NOW transmit PHY and rate. */
#define CSI_RX_ESPNOW_PHYMODE WIFI_PHY_MODE_HT40
#define CSI_RX_ESPNOW_RATE WIFI_PHY_RATE_MCS0_LGI

/* Receive queue capacity in packets: about two seconds at 100 Hz. */
#define CSI_RX_QUEUE_LEN 200
#define CSI_RX_FORCE_LLTF 0

/* 60 seconds at nominal 100 Hz, with 10% capacity headroom. */
#define CSI_RX_WINDOW_SECONDS 60
#define CSI_RX_WINDOW_CAPACITY 6600

/* Default 60s amplitude PSD uses 4096; ACF reuses it at 2048 points. */
#define CSI_RX_AMP_FFT_CAPACITY 4096
