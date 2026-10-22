/*
 * Occupancy decision core, stage 1. See occupancy_core.h for the contract.
 *
 * Floating point: all accumulation is in double, in packet order, with no
 * reassociation. Build with -ffp-contract=off (host CMake and the component
 * CMakeLists) so the device and the PC produce the same rounding.
 */
#include "occupancy_core.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* A subcarrier whose sum of squared deviations is below this fraction of its
 * sum of squares is treated as constant (the quotient would be noise). */
#define OCC_MIN_RELATIVE_VARIANCE 1e-12


void occ_default_config(occ_config_t *config) {
    memset(config, 0, sizeof(*config));

    config->link_id = 0;
    config->window_packets = 100;
    config->min_packets = 80;

    /* Pair indices 57, 58, 59 are DC and its neighbours: always zero in
     * every recorded session (report 0001). */
    uint16_t count = 0;

    for (uint16_t index = 0; index <= 116; ++index) {
        if (index >= 57 && index <= 59) {
            continue;
        }

        config->subcarrier_indices[count] = (uint8_t)index;
        count++;
    }

    config->subcarrier_count = count;

    config->gain_mode = OCC_GAIN_NORMALIZE;

    config->false_alarm_rate = 1e-3;
    config->effective_subcarriers = 60.0;
    config->empty_mean_times_n = -1.0;

    config->entry_windows = 2;
    config->hold_time_us = 600LL * 1000000LL;

    config->window_duration_us = 1000000;
}

/*
 * Upper-tail standard normal quantile: z with P(Z > z) = p, 0 < p < 0.5.
 * Acklam's rational approximation (relative error below 1.2e-9), lower
 * region formula applied to p and negated. Called once per init.
 */
static double upper_normal_quantile(double p) {
    static const double c[] = {-7.784894002430293e-03, -3.223964580411365e-01,
                               -2.400758277161838e+00, -2.549732539343734e+00,
                               4.374664141464968e+00, 2.938163982698783e+00};
    static const double d[] = {7.784695709041462e-03, 3.224671290700398e-01,
                               2.445134137142996e+00, 3.754408661907416e+00};
    static const double a[] = {-3.969683028665376e+01, 2.209460984245205e+02,
                               -2.759285104469687e+02, 1.383577518672690e+02,
                               -3.066479806614716e+01, 2.506628277459239e+00};
    static const double b[] = {-5.447609879822406e+01, 1.615858368580409e+02,
                               -1.556989798598866e+02, 6.680131188771972e+01,
                               -1.328068155288572e+01};
    const double lower_region = 0.02425;

    double lower_quantile;

    if (p < lower_region) {
        double q = sqrt(-2.0 * log(p));
        double numerator = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5];
        double denominator = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0;

        lower_quantile = numerator / denominator;
    } else {
        double q = p - 0.5;
        double r = q * q;
        double numerator = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q;
        double denominator = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0;

        lower_quantile = numerator / denominator;
    }

    return -lower_quantile;
}

bool occ_core_init(occ_core_t *core, const occ_config_t *config) {
    if (config->window_packets == 0) {
        return false;
    }

    if (config->subcarrier_count == 0 || config->subcarrier_count > OCC_MAX_SUBCARRIERS) {
        return false;
    }

    if (!(config->false_alarm_rate > 0.0 && config->false_alarm_rate < 0.5)) {
        return false;
    }

    if (!(config->effective_subcarriers > 0.0) || config->entry_windows == 0) {
        return false;
    }

    if (config->gain_mode != OCC_GAIN_NONE && config->gain_mode != OCC_GAIN_NORMALIZE) {
        return false;
    }

    if (config->hold_time_us < 0 || config->window_duration_us <= 0) {
        return false;
    }

    uint16_t highest_index = 0;

    for (uint16_t i = 0; i < config->subcarrier_count; ++i) {
        if (config->subcarrier_indices[i] >= OCC_MAX_SUBCARRIERS) {
            return false;
        }

        if (config->subcarrier_indices[i] > highest_index) {
            highest_index = config->subcarrier_indices[i];
        }
    }

    memset(core, 0, sizeof(*core));
    core->config = *config;
    core->required_csi_len = (uint16_t)(2 * (highest_index + 1));
    core->false_alarm_z = upper_normal_quantile(config->false_alarm_rate);
    core->state = OCC_STATE_EMPTY;

    return true;
}

static double subcarrier_power(const int8_t *csi, uint8_t index) {
    double first = (double)csi[2 * index];
    double second = (double)csi[2 * index + 1];

    return first * first + second * second;
}

static void open_window(occ_core_t *core, const occ_csi_record_t *record, bool stream_start) {
    core->window_open = true;
    core->window_index = record->seq / core->config.window_packets;
    core->first_seq = record->seq;
    core->last_seq = record->seq;
    core->first_rx_time_us = record->rx_time_us;
    core->last_rx_time_us = record->rx_time_us;
    core->packets = 0;
    core->pairs = 0;
    core->duplicates = 0;
    core->stream_started_in_window = stream_start;

    memset(core->sums, 0, sizeof(core->sums));
}

/* Divisor applied to every subcarrier power of one packet: 1 without gain
 * handling, else the packet's mean power over the configured subcarriers.
 * Returns 0 when normalization is impossible (all configured powers zero). */
static double packet_scale(const occ_config_t *config, const int8_t *csi) {
    if (config->gain_mode == OCC_GAIN_NONE) {
        return 1.0;
    }

    double total = 0.0;

    for (uint16_t i = 0; i < config->subcarrier_count; ++i) {
        total += subcarrier_power(csi, config->subcarrier_indices[i]);
    }

    return total / (double)config->subcarrier_count;
}

static void add_packet(occ_core_t *core, const occ_csi_record_t *record, double scale) {
    const occ_config_t *config = &core->config;
    bool forms_pair = core->packets > 0 && record->seq == core->last_seq + 1;

    for (uint16_t i = 0; i < config->subcarrier_count; ++i) {
        occ_subcarrier_sums_t *sums = &core->sums[i];
        double power = subcarrier_power(record->csi, config->subcarrier_indices[i]) / scale;

        sums->sum += power;
        sums->sum_squares += power * power;

        if (forms_pair) {
            sums->pair_products += sums->previous * power;
            sums->pair_first += sums->previous;
            sums->pair_second += power;
        }

        sums->previous = power;
    }

    if (forms_pair) {
        core->pairs++;
    }

    core->packets++;
    core->last_seq = record->seq;
    core->last_rx_time_us = record->rx_time_us;
    core->last_packet_time_us = record->rx_time_us;
}

/* Mean over subcarriers of the lag-1 autocorrelation; returns the number of
 * subcarriers that entered the mean. */
static uint16_t motion_statistic(const occ_core_t *core, double *motion) {
    const double packets = (double)core->packets;
    const double pairs = (double)core->pairs;

    double rho_sum = 0.0;
    uint16_t used = 0;

    for (uint16_t i = 0; i < core->config.subcarrier_count; ++i) {
        const occ_subcarrier_sums_t *sums = &core->sums[i];

        double mean = sums->sum / packets;
        double squared_deviations = sums->sum_squares - sums->sum * mean;

        if (squared_deviations <= OCC_MIN_RELATIVE_VARIANCE * sums->sum_squares) {
            continue;
        }

        double cross_deviations = sums->pair_products
                                  - mean * (sums->pair_first + sums->pair_second)
                                  + pairs * mean * mean;

        rho_sum += cross_deviations / squared_deviations;
        used++;
    }

    *motion = used > 0 ? rho_sum / (double)used : 0.0;

    return used;
}

static bool motion_exceeds(double motion, double eta) {
    return motion > eta;
}

static double window_threshold(const occ_core_t *core, uint16_t packets) {
    const occ_config_t *config = &core->config;
    double n = (double)packets;

    return config->empty_mean_times_n / n
           + core->false_alarm_z / sqrt(n * config->effective_subcarriers);
}

static void emit_event(occ_core_t *core, occ_state_t new_state, occ_reason_t reason,
                       int64_t time_us, occ_output_t *output) {
    occ_event_t *event = &output->event;

    memset(event, 0, sizeof(*event));
    event->link_id = core->config.link_id;
    event->window_index = core->previous_window_index;
    event->time_us = time_us;
    event->previous_state = core->state;
    event->new_state = new_state;
    event->reason = reason;
    event->motion_streak = core->motion_streak;
    event->evidence_age_us = time_us - core->last_evidence_us;
    event->last_evidence = core->last_evidence;

    output->has_event = true;
    core->state = new_state;

    /* Motion before a state change never counts toward the next entry. */
    core->motion_streak = 0;
}

/* The exit is stamped with its scheduled time (last evidence + hold), not
 * the time it was noticed, so a device that checks every 100 ms and a replay
 * that checks only when the next record arrives report the same event. */
static void check_hold_time(occ_core_t *core, int64_t now_us, occ_output_t *output) {
    if (core->state != OCC_STATE_OCCUPIED) {
        return;
    }

    int64_t expiry_us = core->last_evidence_us + core->config.hold_time_us;

    if (now_us >= expiry_us) {
        emit_event(core, OCC_STATE_EMPTY, OCC_REASON_HOLD_EXPIRED, expiry_us, output);
    }
}

/* Evidence only moves forward in time: an older window cannot undo newer
 * external evidence. */
static void record_evidence(occ_core_t *core, int64_t time_us, occ_evidence_t source) {
    if (time_us > core->last_evidence_us) {
        core->last_evidence_us = time_us;
        core->last_evidence = source;
    }
}

/* Tracks the receive clock of the records. A record older than the previous
 * one means the receiver clock restarted (board reset): the hold restarts
 * from the new clock value. Only record times can signal this; a time passed
 * to occ_core_advance_time may be an estimate and is never taken as a reset. */
static void observe_record_clock(occ_core_t *core, int64_t record_time_us) {
    if (core->clock_started && record_time_us < core->clock_us) {
        if (core->state == OCC_STATE_OCCUPIED) {
            core->last_evidence_us = record_time_us;
        }
    }

    core->clock_started = true;
    core->clock_us = record_time_us;
}

/* Feeds one closed window into the state machine. */
static void update_state(occ_core_t *core, const occ_stats_t *stats, occ_output_t *output) {
    bool consecutive = core->has_previous_decision
                       && stats->window_index == core->previous_window_index + 1;

    if (stats->decision == OCC_DECISION_MOTION) {
        core->motion_streak = consecutive ? (uint16_t)(core->motion_streak + 1) : 1;
    } else {
        core->motion_streak = 0;
    }

    core->has_previous_decision = true;
    core->previous_window_index = stats->window_index;

    int64_t window_time_us = stats->last_rx_time_us;

    if (stats->decision == OCC_DECISION_MOTION && core->state == OCC_STATE_OCCUPIED) {
        record_evidence(core, window_time_us, OCC_EVIDENCE_MOTION);
        return;
    }

    if (core->state == OCC_STATE_EMPTY) {
        if (core->motion_streak >= core->config.entry_windows) {
            core->last_evidence_us = window_time_us;
            core->last_evidence = OCC_EVIDENCE_MOTION;
            emit_event(core, OCC_STATE_OCCUPIED, OCC_REASON_MOTION_STREAK, window_time_us, output);
        }

        return;
    }

    check_hold_time(core, window_time_us, output);
}

/* `last_expected_seq` is the highest seq the window should have held: the
 * window's last seq when a later window has started, otherwise the last seq
 * actually received (flush or transmitter restart). */
static void close_window(occ_core_t *core, uint32_t last_expected_seq, occ_output_t *output) {
    const occ_config_t *config = &core->config;
    occ_stats_t *stats = &output->stats;

    uint32_t window_first_seq = core->window_index * config->window_packets;
    uint32_t first_expected_seq = core->stream_started_in_window ? core->first_seq : window_first_seq;
    uint32_t expected = last_expected_seq - first_expected_seq + 1;

    memset(stats, 0, sizeof(*stats));
    stats->link_id = config->link_id;
    stats->window_index = core->window_index;
    stats->first_rx_time_us = core->first_rx_time_us;
    stats->last_rx_time_us = core->last_rx_time_us;
    stats->packets = core->packets;
    stats->pairs = core->pairs;
    stats->lost = (uint16_t)(expected - core->packets);
    stats->duplicates = (uint16_t)(core->duplicates + core->dropped_since_window);
    core->dropped_since_window = 0;

    if (core->packets < config->min_packets) {
        stats->status = OCC_WINDOW_TOO_FEW_PACKETS;
    } else if (core->pairs == 0) {
        stats->status = OCC_WINDOW_NO_ESTIMATE;
    } else {
        double motion = 0.0;
        uint16_t used = motion_statistic(core, &motion);

        stats->subcarriers = used;

        if (used == 0) {
            stats->status = OCC_WINDOW_NO_ESTIMATE;
        } else {
            stats->status = OCC_WINDOW_DECIDABLE;
            stats->motion = motion;
        }
    }

    if (stats->status == OCC_WINDOW_DECIDABLE) {
        stats->eta = window_threshold(core, core->packets);
        stats->decision = motion_exceeds(stats->motion, stats->eta) ? OCC_DECISION_MOTION
                                                                    : OCC_DECISION_NO_MOTION;
    } else {
        stats->eta = 0.0;
        stats->decision = OCC_DECISION_UNDECIDABLE;
    }

    output->has_stats = true;
    core->window_open = false;
    core->has_emitted_window = true;
    core->last_emitted_window_index = core->window_index;

    update_state(core, stats, output);
}

/* A window in which no packet arrived: undecidable, all seq numbers lost.
 * Its receive times are the time the gap was detected. */
static void emit_empty_window(occ_core_t *core, int64_t detected_us, occ_output_t *output) {
    occ_stats_t *stats = &output->stats;
    uint32_t window_index = core->last_emitted_window_index + 1;

    memset(stats, 0, sizeof(*stats));
    stats->link_id = core->config.link_id;
    stats->window_index = window_index;
    stats->first_rx_time_us = detected_us;
    stats->last_rx_time_us = detected_us;
    stats->lost = core->config.window_packets;
    stats->status = OCC_WINDOW_TOO_FEW_PACKETS;
    stats->decision = OCC_DECISION_UNDECIDABLE;

    output->has_stats = true;
    core->last_emitted_window_index = window_index;

    update_state(core, stats, output);
}

bool occ_core_push(occ_core_t *core, const occ_csi_record_t *record, occ_output_t *output) {
    output->has_stats = false;
    output->has_event = false;

    if (record->csi == NULL || record->csi_len < core->required_csi_len) {
        return false;
    }

    const uint32_t window_packets = core->config.window_packets;

    double scale = packet_scale(&core->config, record->csi);

    if (!(scale > 0.0)) {
        return false;
    }

    observe_record_clock(core, record->rx_time_us);

    /* An exact repeat of the previous seq is a duplicate delivery (the only
     * backward step seen in the recorded sessions). Any other backward step
     * is a transmitter restart and starts a new stream. */
    if (core->stream_started && record->seq == core->last_seq) {
        core->dropped_since_window++;
        return true;
    }

    bool restart = core->stream_started && record->seq < core->last_seq;
    uint32_t window_index = record->seq / window_packets;

    if (!core->stream_started || restart) {
        if (core->window_open) {
            close_window(core, core->last_seq, output);
        }

        core->stream_started = true;
        core->pending_empty_windows = 0;
        core->has_emitted_window = false;

        open_window(core, record, true);
        add_packet(core, record, scale);
        return true;
    }

    if (core->window_open && window_index == core->window_index) {
        add_packet(core, record, scale);
        return true;
    }

    /* A window index that was already reported is never reopened: packets
     * that arrive for it after it closed on silence (transmitter paused) are
     * dropped and counted, so every window index appears once. */
    if (!core->window_open && core->has_emitted_window && window_index <= core->last_emitted_window_index) {
        core->dropped_since_window++;
        core->last_seq = record->seq;
        return true;
    }

    if (core->window_open) {
        uint32_t window_last_seq = (core->window_index + 1) * window_packets - 1;
        close_window(core, window_last_seq, output);
    }

    /* Windows between the last emitted one and this one got no packet at
     * all; occ_core_poll reports them one by one. */
    if (core->has_emitted_window && window_index > core->last_emitted_window_index + 1) {
        core->pending_empty_windows = window_index - core->last_emitted_window_index - 1;
        core->pending_detected_us = record->rx_time_us;
    }

    open_window(core, record, false);
    add_packet(core, record, scale);

    return true;
}

bool occ_core_flush(occ_core_t *core, occ_output_t *output) {
    output->has_stats = false;
    output->has_event = false;

    if (!core->window_open) {
        return false;
    }

    close_window(core, core->last_seq, output);

    return true;
}

bool occ_core_poll(occ_core_t *core, occ_output_t *output) {
    output->has_stats = false;
    output->has_event = false;

    if (core->pending_empty_windows == 0) {
        return false;
    }

    core->pending_empty_windows--;
    emit_empty_window(core, core->pending_detected_us, output);

    return true;
}

/*
 * Advances the receive clock without a record. After one window duration of
 * silence the open window is closed (its motion becomes evidence); no window
 * is invented: which seq windows are missing is only known when packets
 * resume, and occ_core_poll then reports them with their real indices. The
 * hold time keeps running so OCCUPIED expires during silence.
 */
void occ_core_advance_time(occ_core_t *core, int64_t now_us, occ_output_t *output) {
    output->has_stats = false;
    output->has_event = false;

    /* Times before the last record are ignored (estimates on the device). */
    if (core->clock_started && now_us < core->clock_us) {
        return;
    }

    /* After one window duration without packets the open window is closed,
     * so its motion counts as evidence before the hold can expire. */
    if (core->window_open && now_us - core->last_packet_time_us >= core->config.window_duration_us) {
        uint32_t window_last_seq = (core->window_index + 1) * core->config.window_packets - 1;

        close_window(core, window_last_seq, output);
        return;
    }

    check_hold_time(core, now_us, output);
}

void occ_core_set_occupied(occ_core_t *core, int64_t time_us) {
    core->state = OCC_STATE_OCCUPIED;
    core->last_evidence_us = time_us;
    core->last_evidence = OCC_EVIDENCE_MOTION;
    core->motion_streak = 0;
}

void occ_core_add_evidence(occ_core_t *core, int64_t time_us, occ_evidence_t source) {
    if (core->state != OCC_STATE_OCCUPIED) {
        return;
    }

    record_evidence(core, time_us, source);
}

int occ_format_stats(char *buffer, size_t size, const occ_stats_t *stats) {
    return snprintf(buffer, size,
                    "OCC_STATS,%lu,%lu,%lld,%lld,%.8f,%u,%u,%u,%u,%u,%d,%.8f,%d",
                    (unsigned long)stats->link_id,
                    (unsigned long)stats->window_index,
                    (long long)stats->first_rx_time_us,
                    (long long)stats->last_rx_time_us,
                    stats->motion,
                    (unsigned)stats->packets,
                    (unsigned)stats->pairs,
                    (unsigned)stats->lost,
                    (unsigned)stats->duplicates,
                    (unsigned)stats->subcarriers,
                    (int)stats->status,
                    stats->eta,
                    (int)stats->decision);
}

int occ_format_event(char *buffer, size_t size, const occ_event_t *event) {
    return snprintf(buffer, size,
                    "OCC_EVENT,%lu,%lu,%lld,%d,%d,%d,%u,%lld,%d",
                    (unsigned long)event->link_id,
                    (unsigned long)event->window_index,
                    (long long)event->time_us,
                    (int)event->previous_state,
                    (int)event->new_state,
                    (int)event->reason,
                    (unsigned)event->motion_streak,
                    (long long)event->evidence_age_us,
                    (int)event->last_evidence);
}
