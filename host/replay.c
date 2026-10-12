/*
 * Replay a recorded session through the occupancy decision core.
 *
 *   occ_replay [--link-id N] [--window-packets N] [--min-packets N]
 *              [--gain none|normalize] [--feff X] [--false-alarm P]
 *              [--seq-range FIRST:LAST] [--start-occupied] [--no-flush] [FILE]
 *
 * --seq-range keeps only records with FIRST <= seq <= LAST (one segment of a
 * session). --start-occupied puts the core in OCCUPIED at the first kept
 * record (evaluation of sessions that start with a person present).
 * --no-flush leaves the last window open at end of input, as the receiver
 * firmware does (device / PC comparison, ticket 08).
 *
 * Reads CSI_DATA lines (e.g. a session's serial.bin) from FILE or stdin and
 * prints one OCC_STATS line per closed window and one OCC_EVENT line per
 * occupancy state change to stdout. A short summary goes to stderr.
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "csi_line.h"
#include "occupancy_core.h"

#define TIMESTAMP_WRAP_US 4294967296LL

typedef struct {
    unsigned long lines;
    unsigned long csi_records;
    unsigned long rejected_lines;         /**< after the first valid CSI line */
    unsigned long rejected_leading_lines; /**< before it (capture started mid-line) */
    unsigned long rejected_records;
    unsigned long windows;
    unsigned long events;
} replay_counts_t;

/* Extends the RX hardware clock (uint32 microseconds) to 64 bits. A backward
 * step larger than half the range is a wrap; a smaller one (board reset) is
 * passed through unchanged. */
typedef struct {
    bool started;
    uint32_t previous;
    int64_t wraps;
} timestamp_unwrapper_t;

static int64_t unwrap_timestamp(timestamp_unwrapper_t *unwrapper, uint32_t timestamp_us) {
    if (unwrapper->started && timestamp_us < unwrapper->previous
        && unwrapper->previous - timestamp_us > 0x80000000u) {
        unwrapper->wraps++;
    }

    unwrapper->started = true;
    unwrapper->previous = timestamp_us;

    return unwrapper->wraps * TIMESTAMP_WRAP_US + (int64_t)timestamp_us;
}

/* Stats first, then the event the same window caused. */
static void print_output(const occ_output_t *output, replay_counts_t *counts) {
    char line[256];

    if (output->has_stats) {
        occ_format_stats(line, sizeof(line), &output->stats);
        puts(line);
        counts->windows++;
    }

    if (output->has_event) {
        occ_format_event(line, sizeof(line), &output->event);
        puts(line);
        counts->events++;
    }
}

static void usage(const char *program) {
    fprintf(stderr,
            "usage: %s [--link-id N] [--window-packets N] [--min-packets N]"
            " [--gain none|normalize] [--feff X] [--false-alarm P]"
            " [--seq-range FIRST:LAST] [--start-occupied] [--no-flush] [FILE]\n",
            program);
}

int main(int argc, char **argv) {
    occ_config_t config;
    occ_default_config(&config);

    const char *input_path = NULL;
    bool has_seq_range = false;
    uint32_t seq_first = 0;
    uint32_t seq_last = 0;
    bool start_occupied = false;
    bool flush_at_end = true;

    for (int i = 1; i < argc; ++i) {
        bool has_value = i + 1 < argc;

        if (strcmp(argv[i], "--link-id") == 0 && has_value) {
            i++;
            config.link_id = (uint32_t)strtoul(argv[i], NULL, 10);
        } else if (strcmp(argv[i], "--window-packets") == 0 && has_value) {
            i++;
            config.window_packets = (uint16_t)strtoul(argv[i], NULL, 10);
        } else if (strcmp(argv[i], "--min-packets") == 0 && has_value) {
            i++;
            config.min_packets = (uint16_t)strtoul(argv[i], NULL, 10);
        } else if (strcmp(argv[i], "--gain") == 0 && has_value) {
            i++;

            if (strcmp(argv[i], "none") == 0) {
                config.gain_mode = OCC_GAIN_NONE;
            } else if (strcmp(argv[i], "normalize") == 0) {
                config.gain_mode = OCC_GAIN_NORMALIZE;
            } else {
                usage(argv[0]);
                return 2;
            }
        } else if (strcmp(argv[i], "--feff") == 0 && has_value) {
            i++;
            config.effective_subcarriers = strtod(argv[i], NULL);
        } else if (strcmp(argv[i], "--false-alarm") == 0 && has_value) {
            i++;
            config.false_alarm_rate = strtod(argv[i], NULL);
        } else if (strcmp(argv[i], "--seq-range") == 0 && has_value) {
            i++;

            char *separator = NULL;
            seq_first = (uint32_t)strtoul(argv[i], &separator, 10);

            if (separator == NULL || *separator != ':') {
                usage(argv[0]);
                return 2;
            }

            seq_last = (uint32_t)strtoul(separator + 1, NULL, 10);
            has_seq_range = true;
        } else if (strcmp(argv[i], "--no-flush") == 0) {
            flush_at_end = false;
        } else if (strcmp(argv[i], "--start-occupied") == 0) {
            start_occupied = true;
        } else if (argv[i][0] != '-' && input_path == NULL) {
            input_path = argv[i];
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    static occ_core_t core;

    if (!occ_core_init(&core, &config)) {
        fprintf(stderr, "invalid core configuration\n");
        return 2;
    }

    FILE *input = stdin;

    if (input_path != NULL) {
        input = fopen(input_path, "rb");

        if (input == NULL) {
            perror(input_path);
            return 1;
        }
    }

    replay_counts_t counts = {0};
    timestamp_unwrapper_t unwrapper = {0};
    occ_output_t output;
    csi_line_t parsed;

    char *line = NULL;
    size_t capacity = 0;

    while (getline(&line, &capacity, input) != -1) {
        counts.lines++;

        if (strstr(line, "CSI_DATA,") == NULL) {
            continue;
        }

        if (!csi_line_parse(line, &parsed)) {
            if (counts.csi_records == 0) {
                counts.rejected_leading_lines++;
            } else {
                counts.rejected_lines++;
            }

            continue;
        }

        if (has_seq_range && (parsed.seq < seq_first || parsed.seq > seq_last)) {
            continue;
        }

        occ_csi_record_t record = {
            .seq = parsed.seq,
            .rx_time_us = unwrap_timestamp(&unwrapper, parsed.local_timestamp_us),
            .rssi = parsed.rssi,
            .agc_gain = parsed.agc_gain,
            .fft_gain = parsed.fft_gain,
            .csi_len = parsed.decoded_len,
            .csi = parsed.csi,
        };

        counts.csi_records++;

        if (start_occupied) {
            occ_core_set_occupied(&core, record.rx_time_us);
            start_occupied = false;
        }

        /* Same calls the firmware makes: let silence close windows first. */
        do {
            occ_core_advance_time(&core, record.rx_time_us, &output);
            print_output(&output, &counts);
        } while (output.has_stats || output.has_event);

        if (!occ_core_push(&core, &record, &output)) {
            counts.rejected_records++;
            continue;
        }

        print_output(&output, &counts);

        while (occ_core_poll(&core, &output)) {
            print_output(&output, &counts);
        }
    }

    if (flush_at_end) {
        occ_core_flush(&core, &output);
        print_output(&output, &counts);
    }

    free(line);

    if (input != stdin) {
        fclose(input);
    }

    fprintf(stderr,
            "lines=%lu csi_records=%lu rejected_leading_lines=%lu rejected_lines=%lu"
            " rejected_records=%lu windows=%lu events=%lu\n",
            counts.lines, counts.csi_records, counts.rejected_leading_lines, counts.rejected_lines,
            counts.rejected_records, counts.windows, counts.events);

    return 0;
}
