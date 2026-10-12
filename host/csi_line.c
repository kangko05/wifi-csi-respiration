#include "csi_line.h"

#include <stdlib.h>
#include <string.h>

#define CSI_FIELD_COUNT 15
#define CSI_DATA_FIELD 14

static int base64_value(char symbol) {
    if (symbol >= 'A' && symbol <= 'Z') {
        return symbol - 'A';
    }

    if (symbol >= 'a' && symbol <= 'z') {
        return symbol - 'a' + 26;
    }

    if (symbol >= '0' && symbol <= '9') {
        return symbol - '0' + 52;
    }

    if (symbol == '+') {
        return 62;
    }

    if (symbol == '/') {
        return 63;
    }

    return -1;
}

/* Decodes standard base64 up to the first character outside the alphabet.
 * Returns the decoded byte count, or -1 on overflow / bad length. */
static int base64_decode(const char *text, int8_t *out, int capacity) {
    uint32_t accumulator = 0;
    int bits = 0;
    int written = 0;

    for (const char *cursor = text; *cursor != '\0'; ++cursor) {
        if (*cursor == '=' || *cursor == '\r' || *cursor == '\n') {
            break;
        }

        int value = base64_value(*cursor);

        if (value < 0) {
            return -1;
        }

        accumulator = (accumulator << 6) | (uint32_t)value;
        bits += 6;

        if (bits >= 8) {
            bits -= 8;

            if (written >= capacity) {
                return -1;
            }

            out[written] = (int8_t)(uint8_t)((accumulator >> bits) & 0xff);
            written++;
        }
    }

    return written;
}

bool csi_line_parse(const char *line, csi_line_t *parsed) {
    const char *start = strstr(line, "CSI_DATA,");

    if (start == NULL) {
        return false;
    }

    const char *fields[CSI_FIELD_COUNT];
    int field_count = 0;
    const char *cursor = start;

    fields[field_count] = cursor;
    field_count++;

    while (*cursor != '\0' && field_count < CSI_FIELD_COUNT) {
        if (*cursor == ',') {
            fields[field_count] = cursor + 1;
            field_count++;
        }

        cursor++;
    }

    if (field_count != CSI_FIELD_COUNT) {
        return false;
    }

    memset(parsed, 0, sizeof(*parsed));
    parsed->seq = (uint32_t)strtoul(fields[1], NULL, 10);
    parsed->rssi = (int8_t)strtol(fields[2], NULL, 10);
    parsed->noise_floor = (int8_t)strtol(fields[3], NULL, 10);
    parsed->fft_gain = (int8_t)strtol(fields[4], NULL, 10);
    parsed->agc_gain = (uint8_t)strtoul(fields[5], NULL, 10);
    parsed->local_timestamp_us = (uint32_t)strtoul(fields[7], NULL, 10);
    parsed->len = (uint16_t)strtoul(fields[10], NULL, 10);

    int decoded = base64_decode(fields[CSI_DATA_FIELD], parsed->csi, CSI_LINE_MAX_BYTES);

    if (decoded < 0 || decoded != parsed->len) {
        return false;
    }

    parsed->decoded_len = (uint16_t)decoded;

    return true;
}
