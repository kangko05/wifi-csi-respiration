/*
 * ARI2 packet packing (see ari2_packet.h for the layout and its sources).
 */
#include "ari2_packet.h"

#include <string.h>

static const uint8_t ARI2_MAGIC[4] = {'A', 'R', 'I', '2'};

/* Pairs covered by the four bytes that first_word_invalid refers to. */
#define FIRST_WORD_PAIRS 2

/* Source pair index of subcarrier k (-28..28) for the given order. */
static size_t source_pair_of(int k, ari2_source_order_t order) {
    if (order == ARI2_SOURCE_DC_FIRST) {
        if (k >= 0) {
            return (size_t)k;
        }

        return (size_t)(k + ARI2_HT20_BINS);
    }

    return (size_t)(k + ARI2_HT20_K_OFFSET);
}

bool ari2_map_ht20(const int8_t *source, size_t source_bytes, bool first_word_invalid,
                   ari2_source_order_t order, int8_t out_iq[ARI2_HT20_IQ_BYTES]) {
    if (source == NULL || out_iq == NULL || source_bytes != ARI2_HT20_SOURCE_BYTES) {
        return false;
    }

    for (int bin = 0; bin < ARI2_HT20_BINS; ++bin) {
        int k = bin - ARI2_HT20_K_OFFSET;
        size_t pair = source_pair_of(k, order);

        /* DC carries no channel estimate (ESP-IDF table: subcarrier 0 is
         * invalid at HT20; for STBC it holds a CSD angle). The pairs behind
         * an invalid first word hold no estimate either. Both become 0 so
         * the PC drops them like guard bins. */
        bool unusable = (k == 0) || (first_word_invalid && pair < FIRST_WORD_PAIRS);

        if (unusable) {
            out_iq[2 * bin] = 0;
            out_iq[2 * bin + 1] = 0;
            continue;
        }

        out_iq[2 * bin] = source[2 * pair];         /* imaginary */
        out_iq[2 * bin + 1] = source[2 * pair + 1]; /* real */
    }

    return true;
}

static uint8_t *put_u8(uint8_t *cursor, uint8_t value) {
    cursor[0] = value;

    return cursor + 1;
}

static uint8_t *put_u16_le(uint8_t *cursor, uint16_t value) {
    cursor[0] = (uint8_t)(value & 0xffu);
    cursor[1] = (uint8_t)(value >> 8);

    return cursor + 2;
}

static uint8_t *put_u32_le(uint8_t *cursor, uint32_t value) {
    cursor[0] = (uint8_t)(value & 0xffu);
    cursor[1] = (uint8_t)((value >> 8) & 0xffu);
    cursor[2] = (uint8_t)((value >> 16) & 0xffu);
    cursor[3] = (uint8_t)(value >> 24);

    return cursor + 4;
}

size_t ari2_pack_ht20(const ari2_header_t *header, const int8_t iq[ARI2_HT20_IQ_BYTES],
                      uint8_t out[ARI2_HT20_PACKET_BYTES]) {
    /* Written byte by byte, so the result is little endian on any host. */
    uint8_t *cursor = out;

    memcpy(cursor, ARI2_MAGIC, sizeof(ARI2_MAGIC));
    cursor += sizeof(ARI2_MAGIC);

    cursor = put_u8(cursor, header->room);
    cursor = put_u8(cursor, header->node);
    cursor = put_u8(cursor, header->channel);
    cursor = put_u8(cursor, header->flags);

    cursor = put_u32_le(cursor, header->boot_id);
    cursor = put_u32_le(cursor, header->packet_seq);
    cursor = put_u32_le(cursor, header->sample_seq);
    cursor = put_u32_le(cursor, header->timestamp_us);
    cursor = put_u32_le(cursor, 0);
    cursor = put_u32_le(cursor, 0);

    cursor = put_u8(cursor, (uint8_t)header->rssi);
    cursor = put_u8(cursor, header->agc_gain);
    cursor = put_u8(cursor, (uint8_t)header->fft_gain);
    cursor = put_u8(cursor, (uint8_t)header->tx_power_quarter_dbm);

    cursor = put_u16_le(cursor, ARI2_HT20_IQ_BYTES);
    cursor = put_u16_le(cursor, ARI2_HT20_BINS);

    memcpy(cursor, iq, ARI2_HT20_IQ_BYTES);
    cursor += ARI2_HT20_IQ_BYTES;

    return (size_t)(cursor - out);
}
