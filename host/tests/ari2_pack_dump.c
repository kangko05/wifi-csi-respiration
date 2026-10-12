/*
 * Packs synthetic HT20 CSI frames with the firmware's ARI2 code
 * (csi-rx/main/ari2_packet.c) and writes the packets to stdout, back to
 * back. host/tests/test_ari2_packet.py compiles this file together with the
 * firmware source and checks the packets with the PC parser.
 *
 * Synthetic CSI: the pair that holds subcarrier k is [imag, real] =
 * [k, SYNTHETIC_REAL_OFFSET + k], so every bin shows which subcarrier landed
 * in it. The DC pair holds DC_GARBAGE_* to check that it is zeroed.
 *
 * Packets, in order (the Python test relies on this order):
 *   0  ascending source order
 *   1  DC-first source order (ESP-IDF table order)
 *   2  ascending, first_word_invalid set
 * A 234-byte (HT40) buffer must be rejected; the program exits with 1 if not.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "ari2_packet.h"

#define SYNTHETIC_REAL_OFFSET 40
#define DC_GARBAGE_IMAG 99
#define DC_GARBAGE_REAL (-99)
#define HT40_SOURCE_BYTES 234

/* Header values; the Python test expects exactly these. */
#define TEST_ROOM 1
#define TEST_NODE 1
#define TEST_CHANNEL 36
#define TEST_BOOT_ID 0xA1B2C3D4u
#define TEST_FIRST_SEQ 4000000000u
#define TEST_TIMESTAMP_US 0xFFFFFFF0u
#define TEST_RSSI (-61)
#define TEST_AGC 35
#define TEST_FFT (-3)

/* Subcarrier k held by source pair `pair` in the given order. */
static int subcarrier_of_pair(int pair, ari2_source_order_t order) {
    if (order == ARI2_SOURCE_DC_FIRST) {
        if (pair <= ARI2_HT20_K_OFFSET) {
            return pair;
        }

        return pair - ARI2_HT20_BINS;
    }

    return pair - ARI2_HT20_K_OFFSET;
}

static void fill_source(int8_t source[ARI2_HT20_SOURCE_BYTES], ari2_source_order_t order) {
    for (int pair = 0; pair < ARI2_HT20_BINS; ++pair) {
        int k = subcarrier_of_pair(pair, order);

        if (k == 0) {
            source[2 * pair] = DC_GARBAGE_IMAG;
            source[2 * pair + 1] = DC_GARBAGE_REAL;
            continue;
        }

        source[2 * pair] = (int8_t)k;
        source[2 * pair + 1] = (int8_t)(SYNTHETIC_REAL_OFFSET + k);
    }
}

static bool write_packet(ari2_source_order_t order, bool first_word_invalid, uint32_t seq) {
    int8_t source[ARI2_HT20_SOURCE_BYTES];
    int8_t iq[ARI2_HT20_IQ_BYTES];
    uint8_t packet[ARI2_HT20_PACKET_BYTES];

    fill_source(source, order);

    if (!ari2_map_ht20(source, sizeof(source), first_word_invalid, order, iq)) {
        fprintf(stderr, "ari2_map_ht20 rejected a valid HT20 buffer\n");
        return false;
    }

    ari2_header_t header = {
        .room = TEST_ROOM,
        .node = TEST_NODE,
        .channel = TEST_CHANNEL,
        .flags = 0,
        .boot_id = TEST_BOOT_ID,
        .packet_seq = seq,
        .sample_seq = seq,
        .timestamp_us = TEST_TIMESTAMP_US,
        .rssi = TEST_RSSI,
        .agc_gain = TEST_AGC,
        .fft_gain = TEST_FFT,
        .tx_power_quarter_dbm = 0,
    };

    size_t written = ari2_pack_ht20(&header, iq, packet);

    if (written != ARI2_HT20_PACKET_BYTES) {
        fprintf(stderr, "ari2_pack_ht20 wrote %zu bytes\n", written);
        return false;
    }

    return fwrite(packet, 1, written, stdout) == written;
}

static bool rejects_ht40_buffer(void) {
    int8_t source[HT40_SOURCE_BYTES] = {0};
    int8_t iq[ARI2_HT20_IQ_BYTES];

    return !ari2_map_ht20(source, sizeof(source), false, ARI2_SOURCE_ASCENDING, iq);
}

int main(void) {
    bool ok = true;

    ok = ok && write_packet(ARI2_SOURCE_ASCENDING, false, TEST_FIRST_SEQ);
    ok = ok && write_packet(ARI2_SOURCE_DC_FIRST, false, TEST_FIRST_SEQ + 1);
    ok = ok && write_packet(ARI2_SOURCE_ASCENDING, true, TEST_FIRST_SEQ + 2);

    if (!rejects_ht40_buffer()) {
        fprintf(stderr, "a 234-byte buffer was accepted as HT20\n");
        ok = false;
    }

    fflush(stdout);

    return ok ? 0 : 1;
}
