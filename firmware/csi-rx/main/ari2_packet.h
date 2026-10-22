/*
 * ARI2 packet packing for the PC breathing pipeline (report 0014).
 *
 * Plain C without ESP-IDF dependencies, so the same file is compiled by the
 * host test (host/tests/test_ari2_packet.py).
 *
 * Packet = 40-byte little-endian header + int8 IQ, [imag, real] per bin.
 * The header layout is the Python struct "<4sBBBBIIIIIIbBbbHH" of the PC
 * parser (csi_presence/ari2.py, HEADER and parse_packet):
 *
 *   offset  size  field
 *    0      4     magic "ARI2"
 *    4      1     room
 *    5      1     node
 *    6      1     channel
 *    7      1     flags
 *    8      4     boot id
 *   12      4     packet seq
 *   16      4     sample seq
 *   20      4     ESP timestamp, microseconds, wraps at 2^32
 *   24      4     unused (0)
 *   28      4     unused (0)
 *   32      1     rssi, dBm (int8)
 *   33      1     AGC gain (uint8)
 *   34      1     FFT gain (int8)
 *   35      1     TX power, quarter dBm (int8)
 *   36      2     IQ bytes (= 2 * bins)
 *   38      2     bins
 *
 * Bins: 57 HT20 subcarriers k = -28..+28 in ascending frequency order, bin
 * index = k + 28. The DC bin (k = 0) is always 0: the ESP32-C5 marks
 * subcarrier 0 invalid for 802.11n 20 MHz, and the PC drops all-zero bins.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ARI2_HEADER_BYTES 40
#define ARI2_HT20_BINS 57
#define ARI2_HT20_IQ_BYTES (2 * ARI2_HT20_BINS)
#define ARI2_HT20_PACKET_BYTES (ARI2_HEADER_BYTES + ARI2_HT20_IQ_BYTES)

/* Lowest subcarrier index k of bin 0; bin index = k + ARI2_HT20_K_OFFSET. */
#define ARI2_HT20_K_OFFSET 28

/* HT-LTF CSI of one HT20 frame as the C5 driver delivers it: 57 pairs. */
#define ARI2_HT20_SOURCE_BYTES 114

/*
 * Order of the 57 pairs in the driver's CSI buffer for an HT20 HT-LTF frame
 * received without a secondary channel.
 *
 * ARI2_SOURCE_ASCENDING: pair j is subcarrier k = j - 28 (DC at pair 28).
 *     What this repository's HT40 data implies: in all 45 HT40 sessions the
 *     only zero pairs are 57..59, the middle of 117, i.e. ascending order
 *     with DC at the centre (report 0001), not the table order below.
 * ARI2_SOURCE_DC_FIRST: pairs 0..28 are k = 0..28, pairs 29..56 are
 *     k = -28..-1. The ESP-IDF v6.0.3 table order for the C5 ("0~28,
 *     -28~-1", api-guides/wifi-driver/wifi-vendor-features.rst).
 */
typedef enum {
    ARI2_SOURCE_ASCENDING = 0,
    ARI2_SOURCE_DC_FIRST = 1,
} ari2_source_order_t;

/* Header values of one packet. Unused header words are written as 0. */
typedef struct {
    uint8_t room;
    uint8_t node;
    uint8_t channel;
    uint8_t flags;
    uint32_t boot_id;
    uint32_t packet_seq;
    uint32_t sample_seq;
    uint32_t timestamp_us; /**< low 32 bits of the receive timestamp */
    int8_t rssi;
    uint8_t agc_gain;
    int8_t fft_gain;
    int8_t tx_power_quarter_dbm; /**< 0 when unknown */
} ari2_header_t;

/*
 * Reorders one HT20 CSI buffer into the 57 ARI2 bins.
 *
 * source: driver CSI buffer, int8 [imag, real] pairs in `order`.
 * source_bytes: must be ARI2_HT20_SOURCE_BYTES, otherwise nothing is written
 *     and false is returned (another frame format, e.g. HT40 or LLTF).
 * first_word_invalid: wifi_csi_info_t.first_word_invalid. The first four
 *     source bytes (pairs 0 and 1) are then unusable and their bins are 0.
 * out_iq: ARI2_HT20_IQ_BYTES bytes, [imag, real] per bin, bin = k + 28.
 */
bool ari2_map_ht20(const int8_t *source, size_t source_bytes, bool first_word_invalid,
                   ari2_source_order_t order, int8_t out_iq[ARI2_HT20_IQ_BYTES]);

/*
 * Writes one complete packet (header + 57 bins) to `out`, which must hold
 * ARI2_HT20_PACKET_BYTES bytes. Returns the number of bytes written.
 */
size_t ari2_pack_ht20(const ari2_header_t *header, const int8_t iq[ARI2_HT20_IQ_BYTES],
                      uint8_t out[ARI2_HT20_PACKET_BYTES]);
