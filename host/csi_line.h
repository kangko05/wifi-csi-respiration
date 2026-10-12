/*
 * Parser for the receiver's raw CSI text line (collector format):
 *   CSI_DATA,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,
 *   sig_len,rx_format,len,first_word,compensate_gain,dropped,<base64 csi>
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define CSI_LINE_MAX_BYTES 512

typedef struct {
    uint32_t seq;
    int8_t rssi;
    int8_t noise_floor;
    int8_t fft_gain;
    uint8_t agc_gain;
    uint32_t local_timestamp_us; /**< RX clock, wraps at 2^32 us */
    uint16_t len;                /**< declared CSI bytes */
    uint16_t decoded_len;        /**< bytes actually decoded into csi */
    int8_t csi[CSI_LINE_MAX_BYTES];
} csi_line_t;

/* Parses `line` (NUL-terminated, may carry leading noise before CSI_DATA).
 * Returns false for non-CSI lines and malformed CSI lines. */
bool csi_line_parse(const char *line, csi_line_t *parsed);
