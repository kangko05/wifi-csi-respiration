/*
 * Synthetic tests for the occupancy decision core. They use only the public
 * interface: records in, stats out (spec 0001, testing decisions).
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "occupancy_core.h"

#define CSI_BYTES 234
#define PAIR_COUNT (CSI_BYTES / 2)
#define WINDOW 100
#define TWO_PI 6.283185307179586

static int g_failures = 0;

#define CHECK(condition, ...)                                   \
    do {                                                        \
        if (!(condition)) {                                     \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
            fprintf(stderr, __VA_ARGS__);                       \
            fprintf(stderr, "\n");                              \
            g_failures++;                                       \
        }                                                       \
    } while (0)

/* Deterministic pseudo-random numbers so failures are reproducible. */
static uint64_t g_random_state = 12345;

static double uniform01(void) {
    g_random_state = g_random_state * 6364136223846793005ULL + 1442695040888963407ULL;
    return ((double)(g_random_state >> 11) + 0.5) / 9007199254740992.0;
}

static double gaussian(void) {
    double radius = sqrt(-2.0 * log(uniform01()));
    double angle = TWO_PI * uniform01();

    return radius * cos(angle);
}

static int8_t clamp_int8(double value) {
    double rounded = floor(value + 0.5);

    if (rounded > 127.0) {
        return 127;
    }

    if (rounded < -128.0) {
        return -128;
    }

    return (int8_t)rounded;
}

/* Amplitude model per packet: a_k(t) for each pair index k. The amplitude is
 * written to the first byte of the pair and the second byte is zero, so the
 * subcarrier power is the squared int8 amplitude. */
typedef double (*amplitude_fn)(int packet, int pair_index);

static void fill_csi(int8_t *csi, int packet, amplitude_fn amplitude) {
    memset(csi, 0, CSI_BYTES);

    for (int k = 0; k < PAIR_COUNT; ++k) {
        if (k >= 57 && k <= 59) {
            continue;
        }

        csi[2 * k] = clamp_int8(amplitude(packet, k));
    }
}

static double white_noise_amplitude(int packet, int pair_index) {
    (void)packet;
    (void)pair_index;

    return 60.0 + 3.0 * gaussian();
}

/* Slow power change (0.5 Hz at 100 Hz sampling) with a per-subcarrier phase,
 * like a person moving in the room, plus the same white noise. */
static double slow_change_amplitude(int packet, int pair_index) {
    double time_s = packet / 100.0;
    double phase = 0.37 * pair_index;

    return 60.0 + 20.0 * sin(TWO_PI * 0.5 * time_s + phase) + 3.0 * gaussian();
}

/* Ticket 02 tests check the raw-power statistic, so gain handling is off. */
static void init_core(occ_core_t *core) {
    occ_config_t config;
    occ_default_config(&config);
    config.link_id = 7;
    config.gain_mode = OCC_GAIN_NONE;

    if (!occ_core_init(core, &config)) {
        fprintf(stderr, "core init failed\n");
        exit(1);
    }
}

/* Push packets with the given seq numbers; collect up to `capacity` stats. */
static int run(occ_core_t *core, const uint32_t *seqs, int count, amplitude_fn amplitude,
               occ_stats_t *collected, int capacity) {
    static int8_t csi[CSI_BYTES];
    occ_output_t output;
    int collected_count = 0;

    for (int i = 0; i < count; ++i) {
        fill_csi(csi, i, amplitude);

        occ_csi_record_t record = {
            .seq = seqs[i],
            .rx_time_us = (int64_t)i * 10000,
            .csi_len = CSI_BYTES,
            .csi = csi,
        };

        bool accepted = occ_core_push(core, &record, &output);
        CHECK(accepted, "record %d rejected", i);

        if (output.has_stats && collected_count < capacity) {
            collected[collected_count] = output.stats;
            collected_count++;
        }
    }

    if (occ_core_flush(core, &output) && collected_count < capacity) {
        collected[collected_count] = output.stats;
        collected_count++;
    }

    return collected_count;
}

static void test_white_noise_is_near_zero(void) {
    enum { WINDOWS = 200 };
    static uint32_t seqs[WINDOWS * WINDOW];
    static occ_stats_t stats[WINDOWS + 1];
    static occ_core_t core;

    for (int i = 0; i < WINDOWS * WINDOW; ++i) {
        seqs[i] = (uint32_t)i;
    }

    init_core(&core);
    int count = run(&core, seqs, WINDOWS * WINDOW, white_noise_amplitude, stats, WINDOWS + 1);
    CHECK(count == WINDOWS, "expected %d windows, got %d", WINDOWS, count);

    double sum = 0.0;
    double largest = 0.0;

    for (int w = 0; w < count; ++w) {
        CHECK(stats[w].status == OCC_WINDOW_DECIDABLE, "window %d status %d", w, stats[w].status);
        sum += stats[w].motion;

        if (fabs(stats[w].motion) > largest) {
            largest = fabs(stats[w].motion);
        }
    }

    double mean = sum / count;

    /* Expected about -1/T = -0.01 with spread 1/sqrt(T * 114) ~ 0.0094. */
    CHECK(mean > -0.02 && mean < 0.0, "white noise mean %f not near -1/T", mean);
    CHECK(largest < 0.06, "white noise window reached %f", largest);
}

static void test_slow_change_is_clearly_positive(void) {
    enum { WINDOWS = 10 };
    static uint32_t seqs[WINDOWS * WINDOW];
    static occ_stats_t stats[WINDOWS + 1];
    static occ_core_t core;

    for (int i = 0; i < WINDOWS * WINDOW; ++i) {
        seqs[i] = (uint32_t)i;
    }

    init_core(&core);
    int count = run(&core, seqs, WINDOWS * WINDOW, slow_change_amplitude, stats, WINDOWS + 1);
    CHECK(count == WINDOWS, "expected %d windows, got %d", WINDOWS, count);

    for (int w = 0; w < count; ++w) {
        CHECK(stats[w].status == OCC_WINDOW_DECIDABLE, "window %d status %d", w, stats[w].status);
        CHECK(stats[w].motion > 0.5, "slow change window %d motion %f", w, stats[w].motion);
    }
}

/* Reference motion statistic for one window, written directly from the
 * definition, used to check that no pair is formed across a seq gap. */
static double reference_motion(const uint32_t *seqs, int count, amplitude_fn amplitude,
                               uint64_t random_seed) {
    static double power[WINDOW][PAIR_COUNT];
    static int8_t csi[CSI_BYTES];

    g_random_state = random_seed;

    for (int i = 0; i < count; ++i) {
        fill_csi(csi, i, amplitude);

        for (int k = 0; k < PAIR_COUNT; ++k) {
            double value = csi[2 * k];
            power[i][k] = value * value;
        }
    }

    double rho_sum = 0.0;
    int used = 0;

    for (int k = 0; k < PAIR_COUNT; ++k) {
        if (k >= 57 && k <= 59) {
            continue;
        }

        double mean = 0.0;

        for (int i = 0; i < count; ++i) {
            mean += power[i][k];
        }

        mean /= count;

        double numerator = 0.0;
        double denominator = 0.0;

        for (int i = 0; i < count; ++i) {
            denominator += (power[i][k] - mean) * (power[i][k] - mean);
        }

        for (int i = 0; i + 1 < count; ++i) {
            if (seqs[i + 1] == seqs[i] + 1) {
                numerator += (power[i][k] - mean) * (power[i + 1][k] - mean);
            }
        }

        rho_sum += numerator / denominator;
        used++;
    }

    return rho_sum / used;
}

static void test_seq_gap_forms_no_pair(void) {
    /* One window (seq 0..99) with seq 50 missing: 99 packets, 97 pairs. */
    static uint32_t seqs[WINDOW];
    static occ_stats_t stats[2];
    static occ_core_t core;
    int count = 0;

    for (uint32_t seq = 0; seq < WINDOW; ++seq) {
        if (seq == 50) {
            continue;
        }

        seqs[count] = seq;
        count++;
    }

    const uint64_t seed = 777;
    g_random_state = seed;

    init_core(&core);
    int windows = run(&core, seqs, count, slow_change_amplitude, stats, 2);

    CHECK(windows == 1, "expected 1 window, got %d", windows);
    CHECK(stats[0].packets == 99, "packets %u", stats[0].packets);
    CHECK(stats[0].pairs == 97, "pairs %u", stats[0].pairs);
    CHECK(stats[0].lost == 1, "lost %u", stats[0].lost);

    double expected = reference_motion(seqs, count, slow_change_amplitude, seed);
    CHECK(fabs(stats[0].motion - expected) < 1e-9, "motion %.12f, reference %.12f",
          stats[0].motion, expected);
}

static void test_too_few_packets_is_undecidable(void) {
    /* Window 0 has 50 packets (every other seq), window 1 is complete. */
    static uint32_t seqs[WINDOW + WINDOW / 2];
    static occ_stats_t stats[3];
    static occ_core_t core;
    int count = 0;

    for (uint32_t seq = 0; seq < WINDOW; seq += 2) {
        seqs[count] = seq;
        count++;
    }

    for (uint32_t seq = WINDOW; seq < 2 * WINDOW; ++seq) {
        seqs[count] = seq;
        count++;
    }

    init_core(&core);
    int windows = run(&core, seqs, count, white_noise_amplitude, stats, 3);

    CHECK(windows == 2, "expected 2 windows, got %d", windows);
    CHECK(stats[0].status == OCC_WINDOW_TOO_FEW_PACKETS, "status %d", stats[0].status);
    CHECK(stats[0].packets == 50, "packets %u", stats[0].packets);
    CHECK(stats[0].lost == 50, "lost %u", stats[0].lost);
    CHECK(stats[0].pairs == 0, "pairs %u", stats[0].pairs);
    CHECK(stats[0].motion == 0.0, "undecidable motion %f", stats[0].motion);
    CHECK(stats[1].status == OCC_WINDOW_DECIDABLE, "status %d", stats[1].status);
}

static void test_duplicates_and_restart(void) {
    /* 0..99 with seq 10 repeated, then 100..149, then a TX restart at 0..99. */
    static uint32_t seqs[WINDOW + 1 + WINDOW / 2 + WINDOW];
    static occ_stats_t stats[4];
    static occ_core_t core;
    int count = 0;

    for (uint32_t seq = 0; seq < WINDOW; ++seq) {
        seqs[count] = seq;
        count++;

        if (seq == 10) {
            seqs[count] = seq;
            count++;
        }
    }

    for (uint32_t seq = WINDOW; seq < WINDOW + WINDOW / 2; ++seq) {
        seqs[count] = seq;
        count++;
    }

    for (uint32_t seq = 0; seq < WINDOW; ++seq) {
        seqs[count] = seq;
        count++;
    }

    init_core(&core);
    int windows = run(&core, seqs, count, white_noise_amplitude, stats, 4);

    CHECK(windows == 3, "expected 3 windows, got %d", windows);
    CHECK(stats[0].duplicates == 1, "duplicates %u", stats[0].duplicates);
    CHECK(stats[0].packets == 100, "packets %u", stats[0].packets);
    CHECK(stats[0].pairs == 99, "pairs %u", stats[0].pairs);

    /* The window cut short by the restart counts no loss after its last seq. */
    CHECK(stats[1].window_index == 1, "window index %lu", (unsigned long)stats[1].window_index);
    CHECK(stats[1].packets == 50 && stats[1].lost == 0, "packets %u lost %u",
          stats[1].packets, stats[1].lost);
    CHECK(stats[2].window_index == 0 && stats[2].packets == 100, "restart window %lu packets %u",
          (unsigned long)stats[2].window_index, stats[2].packets);
}

static void test_short_record_is_rejected(void) {
    static occ_core_t core;
    static int8_t csi[CSI_BYTES];
    occ_output_t output;

    init_core(&core);

    occ_csi_record_t record = {
        .seq = 0,
        .csi_len = 200,
        .csi = csi,
    };

    CHECK(!occ_core_push(&core, &record, &output), "short record accepted");
    CHECK(!output.has_stats, "short record produced stats");
}

static void test_format_carries_link_id(void) {
    occ_stats_t stats = {
        .link_id = 7,
        .window_index = 12,
        .motion = 0.25,
        .packets = 100,
        .pairs = 99,
    };
    char line[256];

    occ_format_stats(line, sizeof(line), &stats);
    CHECK(strncmp(line, "OCC_STATS,7,12,", 15) == 0, "line '%s'", line);
}

/* ---- Ticket 03: threshold and occupancy state machine ---- */

typedef enum {
    WINDOW_STILL,       /* white noise -> NO_MOTION */
    WINDOW_MOVING,      /* slow change -> MOTION */
    WINDOW_FEW_PACKETS, /* 50 packets -> UNDECIDABLE */
} window_kind_t;

typedef struct {
    occ_stats_t stats[64];
    int stats_count;
    occ_event_t events[8];
    int event_count;
    uint32_t event_after_window[8]; /* window index whose close emitted it */
} collected_t;

static void collect(const occ_output_t *output, collected_t *collected) {
    if (output->has_stats && collected->stats_count < 64) {
        collected->stats[collected->stats_count] = output->stats;
        collected->stats_count++;
    }

    if (output->has_event && collected->event_count < 8) {
        collected->events[collected->event_count] = output->event;
        collected->event_after_window[collected->event_count] = output->event.window_index;
        collected->event_count++;
    }
}

/* Pushes window `window_index` (seq window_index*100 ..). Packet times are
 * seq * 10 ms, so a window's last packet is at (100 * w + 99) * 10 ms. The
 * window closes when the next window's first packet arrives, so callers
 * finish with occ_core_flush. */
static void push_window(occ_core_t *core, uint32_t window_index, window_kind_t kind,
                        collected_t *collected) {
    static int8_t csi[CSI_BYTES];
    occ_output_t output;

    for (uint32_t offset = 0; offset < WINDOW; ++offset) {
        if (kind == WINDOW_FEW_PACKETS && offset % 2 == 1) {
            continue;
        }

        uint32_t seq = window_index * WINDOW + offset;
        amplitude_fn amplitude = kind == WINDOW_MOVING ? slow_change_amplitude : white_noise_amplitude;

        fill_csi(csi, (int)seq, amplitude);

        occ_csi_record_t record = {
            .seq = seq,
            .rx_time_us = (int64_t)seq * 10000,
            .csi_len = CSI_BYTES,
            .csi = csi,
        };

        occ_core_push(core, &record, &output);
        collect(&output, collected);
    }
}

static void init_core_with(occ_core_t *core, uint16_t entry_windows, int64_t hold_time_us) {
    occ_config_t config;
    occ_default_config(&config);
    config.link_id = 3;
    config.entry_windows = entry_windows;
    config.hold_time_us = hold_time_us;

    if (!occ_core_init(core, &config)) {
        fprintf(stderr, "core init failed\n");
        exit(1);
    }
}

static int64_t window_last_time_us(uint32_t window_index) {
    return ((int64_t)window_index * WINDOW + WINDOW - 1) * 10000;
}

static void test_threshold_value(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_window(&core, 0, WINDOW_STILL, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    /* n = 100, p = 1e-3 (z = 3.090232), F_eff = 60:
     * eta = -0.01 + 3.090232 / sqrt(6000) = 0.0298946 */
    CHECK(collected.stats_count == 1, "stats %d", collected.stats_count);
    CHECK(fabs(collected.stats[0].eta - 0.0298946) < 1e-6, "eta %.7f", collected.stats[0].eta);
    CHECK(collected.stats[0].decision == OCC_DECISION_NO_MOTION, "decision %d",
          collected.stats[0].decision);
}

static void test_entry_needs_consecutive_motion(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* moving, still, moving, few-packets, moving, moving -> enter at window 5 */
    const window_kind_t kinds[] = {WINDOW_MOVING, WINDOW_STILL, WINDOW_MOVING,
                                   WINDOW_FEW_PACKETS, WINDOW_MOVING, WINDOW_MOVING, WINDOW_STILL};
    const int window_count = (int)(sizeof(kinds) / sizeof(kinds[0]));

    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    for (int w = 0; w < window_count; ++w) {
        push_window(&core, (uint32_t)w, kinds[w], &collected);
    }

    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.stats_count == window_count, "stats %d", collected.stats_count);
    CHECK(collected.stats[3].decision == OCC_DECISION_UNDECIDABLE, "few-packet decision %d",
          collected.stats[3].decision);
    CHECK(collected.event_count == 1, "events %d", collected.event_count);
    CHECK(collected.event_after_window[0] == 5, "entry after window %lu",
          (unsigned long)collected.event_after_window[0]);
    CHECK(collected.events[0].new_state == OCC_STATE_OCCUPIED, "new state %d",
          collected.events[0].new_state);
    CHECK(collected.events[0].reason == OCC_REASON_MOTION_STREAK, "reason %d",
          collected.events[0].reason);
    CHECK(collected.events[0].motion_streak == 2, "streak %u", collected.events[0].motion_streak);
    CHECK(collected.events[0].time_us == window_last_time_us(5), "entry time %lld",
          (long long)collected.events[0].time_us);
    CHECK(collected.events[0].link_id == 3, "link id %lu", (unsigned long)collected.events[0].link_id);
}

static void test_missing_window_breaks_streak(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    /* Window 1 never arrives: 0 and 2 are not consecutive. */
    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 2, WINDOW_MOVING, &collected);
    push_window(&core, 3, WINDOW_STILL, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.event_count == 0, "events %d", collected.event_count);
}

static void test_hold_time_boundary(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* Hold 5 s = 5 windows. Enter at window 1 (evidence at its last packet,
     * window_last_time_us(1)). Window 5 closes 4 s after the evidence (just
     * before the hold), window 6 closes exactly 5 s after (expires). */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 5LL * 1000000);

    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 1, WINDOW_MOVING, &collected);

    for (uint32_t w = 2; w <= 8; ++w) {
        push_window(&core, w, WINDOW_STILL, &collected);
    }

    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.event_count == 2, "events %d", collected.event_count);
    CHECK(collected.event_after_window[0] == 1, "entry after window %lu",
          (unsigned long)collected.event_after_window[0]);
    CHECK(collected.event_after_window[1] == 6, "exit after window %lu",
          (unsigned long)collected.event_after_window[1]);
    CHECK(collected.events[1].new_state == OCC_STATE_EMPTY, "exit state %d",
          collected.events[1].new_state);
    CHECK(collected.events[1].reason == OCC_REASON_HOLD_EXPIRED, "exit reason %d",
          collected.events[1].reason);
    CHECK(collected.events[1].evidence_age_us == 5LL * 1000000, "evidence age %lld",
          (long long)collected.events[1].evidence_age_us);
}

static void test_motion_restarts_hold(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* Enter at 1, motion again at 4 restarts the hold: exit at 4 + 5 = 9. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 5LL * 1000000);

    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 1, WINDOW_MOVING, &collected);
    push_window(&core, 2, WINDOW_STILL, &collected);
    push_window(&core, 3, WINDOW_STILL, &collected);
    push_window(&core, 4, WINDOW_MOVING, &collected);

    for (uint32_t w = 5; w <= 10; ++w) {
        push_window(&core, w, WINDOW_STILL, &collected);
    }

    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.event_count == 2, "events %d", collected.event_count);
    CHECK(collected.event_after_window[1] == 9, "exit after window %lu",
          (unsigned long)collected.event_after_window[1]);
}

static void test_external_evidence_and_advance_time(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 5LL * 1000000);

    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 1, WINDOW_MOVING, &collected);
    push_window(&core, 2, WINDOW_STILL, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    int64_t entry_time_us = window_last_time_us(1);

    /* Breathing evidence 3 s after entry moves the expiry to entry + 8 s. */
    occ_core_add_evidence(&core, entry_time_us + 3LL * 1000000, OCC_EVIDENCE_BREATHING);

    occ_core_advance_time(&core, entry_time_us + 7LL * 1000000, &output);
    CHECK(!output.has_event, "expired before the extended hold");

    occ_core_advance_time(&core, entry_time_us + 8LL * 1000000, &output);
    CHECK(output.has_event, "did not expire at the extended hold");
    CHECK(output.event.last_evidence == OCC_EVIDENCE_BREATHING, "last evidence %d",
          output.event.last_evidence);

    /* Evidence while EMPTY does not enter. */
    occ_core_add_evidence(&core, entry_time_us + 9LL * 1000000, OCC_EVIDENCE_BREATHING);
    occ_core_advance_time(&core, entry_time_us + 20LL * 1000000, &output);
    CHECK(!output.has_event, "evidence in EMPTY changed the state");
}

/* ---- Review fixes: restart near start, lost windows, silence ---- */

static void push_seq_range(occ_core_t *core, uint32_t first_seq, uint32_t last_seq,
                           int64_t time_offset_us, collected_t *collected) {
    static int8_t csi[CSI_BYTES];
    occ_output_t output;

    for (uint32_t seq = first_seq; seq <= last_seq; ++seq) {
        fill_csi(csi, (int)seq, white_noise_amplitude);

        occ_csi_record_t record = {
            .seq = seq,
            .rx_time_us = time_offset_us + (int64_t)seq * 10000,
            .csi_len = CSI_BYTES,
            .csi = csi,
        };

        occ_core_push(core, &record, &output);
        collect(&output, collected);

        while (occ_core_poll(core, &output)) {
            collect(&output, collected);
        }
    }
}

static void test_restart_right_after_start(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* 0..49, TX restarts, 0..99: two streams, never merged. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_seq_range(&core, 0, 49, 0, &collected);
    push_seq_range(&core, 0, 99, 1000000, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.stats_count == 2, "stats %d", collected.stats_count);
    CHECK(collected.stats[0].packets == 50 && collected.stats[0].duplicates == 0,
          "first stream packets %u duplicates %u", collected.stats[0].packets,
          collected.stats[0].duplicates);
    CHECK(collected.stats[1].packets == 100 && collected.stats[1].pairs == 99,
          "second stream packets %u pairs %u", collected.stats[1].packets, collected.stats[1].pairs);
}

static void test_fully_lost_windows_are_reported(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* 0..99 then 300..399: windows 1 and 2 had no packet. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_seq_range(&core, 0, 99, 0, &collected);
    push_seq_range(&core, 300, 399, 0, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.stats_count == 4, "stats %d", collected.stats_count);

    for (int i = 0; i < collected.stats_count && i < 4; ++i) {
        CHECK(collected.stats[i].window_index == (uint32_t)i, "stats %d window %lu", i,
              (unsigned long)collected.stats[i].window_index);
    }

    CHECK(collected.stats[1].packets == 0 && collected.stats[1].lost == 100,
          "lost window packets %u lost %u", collected.stats[1].packets, collected.stats[1].lost);
    CHECK(collected.stats[2].decision == OCC_DECISION_UNDECIDABLE, "lost window decision %d",
          collected.stats[2].decision);
}

static void test_silence_then_loss_reports_real_windows(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* 0..149, 5 s of silence, then the stream resumes at seq 650 (the
     * transmitter kept counting, the receiver lost everything). Window 1
     * closes after 1 s of silence; windows 2..5 are reported on resume. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_seq_range(&core, 0, 149, 0, &collected);

    for (int64_t step = 1; step <= 50; ++step) {
        occ_core_advance_time(&core, 149LL * 10000 + step * 100000, &output);
        collect(&output, &collected);
    }

    CHECK(collected.stats_count == 2, "silence: expected windows 0 and 1, got %d", collected.stats_count);

    push_seq_range(&core, 650, 699, 0, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.stats_count == 7, "stats %d", collected.stats_count);

    for (int i = 0; i < collected.stats_count && i < 7; ++i) {
        CHECK(collected.stats[i].window_index == (uint32_t)i, "stats %d window %lu", i,
              (unsigned long)collected.stats[i].window_index);
    }

    CHECK(collected.stats[1].lost == 50, "window 1 lost %u", collected.stats[1].lost);
    CHECK(collected.stats[3].packets == 0, "window 3 packets %u", collected.stats[3].packets);
}

static void test_transmitter_pause_never_repeats_an_index(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* 0..49, the transmitter pauses 3 s, then 50..100. Window 0 closes on
     * silence with 50 packets; the late 50..99 are dropped and counted, and
     * seq 100 opens window 1. No index appears twice. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_seq_range(&core, 0, 49, 0, &collected);

    for (int64_t step = 1; step <= 30; ++step) {
        occ_core_advance_time(&core, 49LL * 10000 + step * 100000, &output);
        collect(&output, &collected);
    }

    push_seq_range(&core, 50, 100, 3000000, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.stats_count == 2, "stats %d", collected.stats_count);
    CHECK(collected.stats[0].window_index == 0 && collected.stats[0].packets == 50,
          "window %lu packets %u", (unsigned long)collected.stats[0].window_index,
          collected.stats[0].packets);
    CHECK(collected.stats[1].window_index == 1 && collected.stats[1].duplicates == 50,
          "second window %lu dropped %u", (unsigned long)collected.stats[1].window_index,
          collected.stats[1].duplicates);
}

static void test_silence_does_not_reenter_on_old_motion(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* Motion in windows 0..2 (seq 0..299), silence, a still packet at
     * 1000 s. Window 2 closes after 1 s of silence, so the evidence is 2.99 s
     * and the exit is at 602.99 s. No second entry from old motion. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);

    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 1, WINDOW_MOVING, &collected);
    push_window(&core, 2, WINDOW_MOVING, &collected);

    for (int64_t now = 3000000; now <= 1000LL * 1000000; now += 100000) {
        occ_core_advance_time(&core, now, &output);
        collect(&output, &collected);
    }

    push_seq_range(&core, 300, 300, 1000LL * 1000000 - 300LL * 10000, &collected);
    occ_core_flush(&core, &output);
    collect(&output, &collected);

    CHECK(collected.event_count == 2, "events %d", collected.event_count);
    CHECK(collected.events[0].new_state == OCC_STATE_OCCUPIED, "first event state %d",
          collected.events[0].new_state);
    CHECK(collected.events[1].new_state == OCC_STATE_EMPTY
              && collected.events[1].time_us == window_last_time_us(2) + 600LL * 1000000,
          "exit state %d at %lld", collected.events[1].new_state,
          (long long)collected.events[1].time_us);
}

static int64_t exit_time_with_step(int64_t step_us) {
    static occ_core_t core;
    occ_output_t output;

    init_core_with(&core, 2, 600LL * 1000000);
    occ_core_set_occupied(&core, 2990000);

    for (int64_t now = 2990000 + step_us; now <= 1000LL * 1000000; now += step_us) {
        occ_core_advance_time(&core, now, &output);

        if (output.has_event) {
            return output.event.time_us;
        }
    }

    return -1;
}

static void test_exit_time_does_not_depend_on_call_rate(void) {
    /* Device: every 100 ms. Replay: only when a record arrives 1000 s later. */
    int64_t frequent = exit_time_with_step(100000);
    int64_t rare = exit_time_with_step(1000LL * 1000000 - 2990000);

    CHECK(frequent == 2990000 + 600LL * 1000000, "frequent exit at %lld", (long long)frequent);
    CHECK(rare == frequent, "rare exit at %lld vs %lld", (long long)rare, (long long)frequent);
}

static void test_older_window_does_not_undo_evidence(void) {
    static occ_core_t core;
    static collected_t collected;
    occ_output_t output;

    /* Enter at window 1 (evidence 1.99 s). Breathing evidence at 3.5 s, then
     * window 2 (last packet 2.99 s) closes: the evidence must stay 3.5 s. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 5LL * 1000000);

    push_window(&core, 0, WINDOW_MOVING, &collected);
    push_window(&core, 1, WINDOW_MOVING, &collected);
    push_window(&core, 2, WINDOW_STILL, &collected);

    occ_core_add_evidence(&core, 3500000, OCC_EVIDENCE_BREATHING);
    push_window(&core, 3, WINDOW_STILL, &collected);

    occ_core_advance_time(&core, 8499999, &output);
    CHECK(!output.has_event, "expired before breathing evidence + hold");

    occ_core_advance_time(&core, 8500000, &output);
    CHECK(output.has_event && output.event.time_us == 8500000, "exit %d at %lld",
          output.has_event, (long long)output.event.time_us);
}

/* Calls occ_core_advance_time until it has nothing more to report, as the
 * firmware and the replay harness do; returns whether an event came out. */
static bool advance_until_quiet(occ_core_t *core, int64_t now_us, occ_event_t *event) {
    occ_output_t output;
    bool has_event = false;

    do {
        occ_core_advance_time(core, now_us, &output);

        if (output.has_event) {
            *event = output.event;
            has_event = true;
        }
    } while (output.has_stats || output.has_event);

    return has_event;
}

static void test_clock_reset_restarts_hold(void) {
    static occ_core_t core;
    static collected_t collected;

    /* Evidence at 10 s, a record at 100 s, then the receiver resets and the
     * next record reads 50 s: the hold restarts at 50 s. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);
    occ_core_set_occupied(&core, 10LL * 1000000);

    push_seq_range(&core, 10000, 10000, 100LL * 1000000 - 10000LL * 10000, &collected);
    push_seq_range(&core, 10001, 10001, 50LL * 1000000 - 10001LL * 10000, &collected);

    occ_event_t event;

    CHECK(!advance_until_quiet(&core, 649LL * 1000000, &event), "expired from the old evidence");
    CHECK(advance_until_quiet(&core, 650LL * 1000000, &event) && event.time_us == 650LL * 1000000,
          "exit at %lld", (long long)event.time_us);
}

static void test_estimated_time_behind_records_is_ignored(void) {
    static occ_core_t core;
    static collected_t collected;

    /* A device clock estimate earlier than the last record must not be taken
     * as a receiver reset: the hold still ends at evidence + hold. */
    memset(&collected, 0, sizeof(collected));
    init_core_with(&core, 2, 600LL * 1000000);
    occ_core_set_occupied(&core, 10LL * 1000000);

    push_seq_range(&core, 10000, 10000, 100LL * 1000000 - 10000LL * 10000, &collected);

    occ_event_t event;

    CHECK(!advance_until_quiet(&core, 99LL * 1000000, &event), "event from an estimate behind the records");
    CHECK(advance_until_quiet(&core, 610LL * 1000000, &event) && event.time_us == 610LL * 1000000,
          "exit at %lld", (long long)event.time_us);
}

/* ---- Ticket 04: gain handling ---- */

/* Constant channel with white noise and a receiver gain step in the middle
 * of every window: amplitude x1.3 on every subcarrier at once. */
static double gain_step_amplitude(int packet, int pair_index) {
    (void)pair_index;

    double gain = (packet % WINDOW) < WINDOW / 2 ? 1.0 : 1.3;

    return 60.0 * gain + 3.0 * gaussian();
}

static int count_motion_windows(occ_gain_mode_t gain_mode, amplitude_fn amplitude, int windows,
                                int *decidable) {
    static uint32_t seqs[20 * WINDOW];
    static occ_stats_t stats[21];
    static occ_core_t core;

    occ_config_t config;
    occ_default_config(&config);
    config.gain_mode = gain_mode;

    if (!occ_core_init(&core, &config)) {
        fprintf(stderr, "core init failed\n");
        exit(1);
    }

    for (int i = 0; i < windows * WINDOW; ++i) {
        seqs[i] = (uint32_t)i;
    }

    int count = run(&core, seqs, windows * WINDOW, amplitude, stats, windows + 1);
    int motion = 0;

    *decidable = 0;

    for (int w = 0; w < count; ++w) {
        if (stats[w].decision == OCC_DECISION_MOTION) {
            motion++;
        }

        if (stats[w].decision != OCC_DECISION_UNDECIDABLE) {
            (*decidable)++;
        }
    }

    return motion;
}

static void test_gain_step_absorbed_by_normalization(void) {
    int decidable = 0;

    int raw_motion = count_motion_windows(OCC_GAIN_NONE, gain_step_amplitude, 20, &decidable);
    CHECK(decidable == 20, "raw decidable %d", decidable);
    CHECK(raw_motion == 20, "without gain handling the step should look like motion (%d/20)",
          raw_motion);

    int normalized_motion = count_motion_windows(OCC_GAIN_NORMALIZE, gain_step_amplitude, 20, &decidable);
    CHECK(decidable == 20, "normalized decidable %d", decidable);
    CHECK(normalized_motion == 0, "normalized gain step gave %d motion windows", normalized_motion);
}

static void test_motion_survives_normalization(void) {
    int decidable = 0;
    int motion = count_motion_windows(OCC_GAIN_NORMALIZE, slow_change_amplitude, 10, &decidable);

    CHECK(decidable == 10, "decidable %d", decidable);
    CHECK(motion == 10, "slow change under normalization: %d/10 motion windows", motion);
}

static void test_all_zero_packet_rejected_when_normalizing(void) {
    static occ_core_t core;
    static int8_t csi[CSI_BYTES];
    occ_config_t config;
    occ_output_t output;

    occ_default_config(&config);
    occ_core_init(&core, &config);

    occ_csi_record_t record = {
        .seq = 0,
        .csi_len = CSI_BYTES,
        .csi = csi,
    };

    CHECK(!occ_core_push(&core, &record, &output), "all-zero packet accepted under normalization");
}

int main(void) {
    test_white_noise_is_near_zero();
    test_slow_change_is_clearly_positive();
    test_seq_gap_forms_no_pair();
    test_too_few_packets_is_undecidable();
    test_duplicates_and_restart();
    test_short_record_is_rejected();
    test_format_carries_link_id();
    test_threshold_value();
    test_entry_needs_consecutive_motion();
    test_missing_window_breaks_streak();
    test_hold_time_boundary();
    test_motion_restarts_hold();
    test_external_evidence_and_advance_time();
    test_restart_right_after_start();
    test_fully_lost_windows_are_reported();
    test_silence_then_loss_reports_real_windows();
    test_transmitter_pause_never_repeats_an_index();
    test_silence_does_not_reenter_on_old_motion();
    test_exit_time_does_not_depend_on_call_rate();
    test_older_window_does_not_undo_evidence();
    test_clock_reset_restarts_hold();
    test_estimated_time_behind_records_is_ignored();
    test_gain_step_absorbed_by_normalization();
    test_motion_survives_normalization();
    test_all_zero_packet_rejected_when_normalizing();

    if (g_failures > 0) {
        fprintf(stderr, "%d check(s) failed\n", g_failures);
        return 1;
    }

    printf("all occupancy core tests passed\n");
    return 0;
}
