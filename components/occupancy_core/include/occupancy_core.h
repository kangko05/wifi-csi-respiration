/*
 * Occupancy decision core (stage 1): CSI records in, 1 s statistics out.
 *
 * Pure C99, no ESP-IDF dependency. The receiver firmware and the PC replay
 * harness compile the same sources (spec 0001).
 *
 * Windowing
 *   Windows are non-overlapping runs of `window_packets` TX sequence numbers:
 *   window index = seq / window_packets. At 100 Hz with window_packets = 100
 *   one window is 1 s of TX time. Using seq instead of the receive clock makes
 *   the window boundaries identical on the device and in PC replay.
 *
 * Motion statistic
 *   For each configured subcarrier k, x_k(t) = |H_k(t)|^2. Inside a window the
 *   lag-1 autocorrelation is
 *       rho_k = sum_pairs (x_t - m)(x_{t+1} - m) / sum_t (x_t - m)^2
 *   where m is the window mean of x_k and a pair is two packets whose seq
 *   numbers differ by exactly 1. The statistic is the mean of rho_k over the
 *   subcarriers with non-zero variance. An empty room gives about -1/T
 *   (report 0001), motion gives clearly positive values.
 *
 * Gain handling (report 0001, ticket 04)
 *   OCC_GAIN_NONE: x_k = |H_k|^2 as received (use when RX gain is locked).
 *   OCC_GAIN_NORMALIZE: x_k = |H_k|^2 / mean over configured subcarriers of
 *   |H_j|^2 of the same packet. A gain step scales every subcarrier of a
 *   packet alike and cancels; motion changes subcarriers differently and
 *   stays. A packet whose configured subcarriers are all zero is rejected.
 *
 * Motion decision (per window, n = packets in the window)
 *   eta = empty_mean_times_n / n + z / sqrt(n * effective_subcarriers)
 *   with z the upper standard normal quantile of `false_alarm_rate`. The
 *   window is MOTION when the statistic exceeds eta, NO_MOTION otherwise,
 *   and UNDECIDABLE when the statistic could not be estimated.
 *
 * Occupancy state machine (EMPTY / OCCUPIED, starts EMPTY)
 *   EMPTY -> OCCUPIED: `entry_windows` consecutive MOTION windows. Windows
 *     are consecutive when their indices differ by 1. A NO_MOTION or
 *     UNDECIDABLE window, or a missing window index, resets the count.
 *   OCCUPIED -> EMPTY: `hold_time_us` has passed since the last occupancy
 *     evidence. Evidence is a MOTION window (time = its last packet) or an
 *     external evidence call (stage-2 breathing detection, ADR 0002); the
 *     evidence time only moves forward. Elapsed time is checked whenever a
 *     window closes and on occ_core_advance_time, against the receive clock.
 *     The exit event is stamped last evidence + hold time. If the receive
 *     clock steps back (board reset), the hold restarts from the new time.
 *
 * Memory
 *   Only a fixed set of running sums per subcarrier (ADR 0001). No raw CSI
 *   window is buffered.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    OCC_GAIN_NONE = 0,
    OCC_GAIN_NORMALIZE = 1,
} occ_gain_mode_t;

/* Upper bound on subcarriers per packet (HT40 rx_format 2 gives 117 pairs). */
#define OCC_MAX_SUBCARRIERS 128

/* One received CSI packet. `csi` holds `csi_len` bytes of int8 pairs in the
 * order the Wi-Fi driver delivers them; pair i is subcarrier index i. */
typedef struct {
    uint32_t seq;          /**< TX payload counter */
    int64_t rx_time_us;    /**< receive time, monotonic microseconds */
    int8_t rssi;           /**< dBm */
    uint8_t agc_gain;      /**< reported RX AGC gain */
    int8_t fft_gain;       /**< reported RX FFT gain */
    uint16_t csi_len;      /**< bytes in `csi` (2 per subcarrier) */
    const int8_t *csi;
} occ_csi_record_t;

typedef struct {
    uint32_t link_id;          /**< copied into every output */
    uint16_t window_packets;   /**< T: seq numbers per window */
    uint16_t min_packets;      /**< fewer received packets -> undecidable */

    /** Subcarrier (pair) indices used by the statistic. */
    uint16_t subcarrier_count;
    uint8_t subcarrier_indices[OCC_MAX_SUBCARRIERS];

    occ_gain_mode_t gain_mode;

    /* Threshold eta (see header comment). */
    double false_alarm_rate;      /**< per-window target, 0 < p < 0.5 */
    double effective_subcarriers; /**< F_eff, calibrated on empty rooms */
    double empty_mean_times_n;    /**< empty-room mean times n (-1 = -1/n) */

    /* State machine. */
    uint16_t entry_windows;       /**< consecutive MOTION windows to enter */
    int64_t hold_time_us;         /**< evidence-free time before EMPTY */

    /** Nominal window length on the receive clock (window_packets x TX
     *  period). The open window is closed after this much silence. */
    int64_t window_duration_us;
} occ_config_t;

typedef enum {
    OCC_WINDOW_DECIDABLE = 0,
    OCC_WINDOW_TOO_FEW_PACKETS = 1, /**< packets < min_packets */
    OCC_WINDOW_NO_ESTIMATE = 2,     /**< no lag-1 pair or no subcarrier variance */
} occ_window_status_t;

typedef enum {
    OCC_DECISION_NO_MOTION = 0,
    OCC_DECISION_MOTION = 1,
    OCC_DECISION_UNDECIDABLE = 2,
} occ_decision_t;

typedef enum {
    OCC_STATE_EMPTY = 0,
    OCC_STATE_OCCUPIED = 1,
} occ_state_t;

typedef enum {
    OCC_REASON_MOTION_STREAK = 0, /**< EMPTY -> OCCUPIED */
    OCC_REASON_HOLD_EXPIRED = 1,  /**< OCCUPIED -> EMPTY */
} occ_reason_t;

/* Evidence sources that can extend the hold time. */
typedef enum {
    OCC_EVIDENCE_MOTION = 0,
    OCC_EVIDENCE_BREATHING = 1, /**< stage 2, not produced in stage 1 */
} occ_evidence_t;

/* One closed window. */
typedef struct {
    uint32_t link_id;
    uint32_t window_index;    /**< seq / window_packets */
    int64_t first_rx_time_us; /**< receive time of the first packet */
    int64_t last_rx_time_us;  /**< receive time of the last packet */
    double motion;            /**< motion statistic; 0 when not decidable */
    uint16_t packets;         /**< distinct packets received */
    uint16_t pairs;           /**< lag-1 pairs used */
    uint16_t lost;            /**< expected seq numbers not received */
    uint16_t duplicates;      /**< dropped: repeated seq, or late for a closed window */
    uint16_t subcarriers;     /**< subcarriers that entered the mean */
    occ_window_status_t status;
    double eta;               /**< threshold for this window; 0 when undecidable */
    occ_decision_t decision;
} occ_stats_t;

/* One occupancy state change. */
typedef struct {
    uint32_t link_id;
    uint32_t window_index;    /**< window that triggered it (last closed one) */
    int64_t time_us;          /**< receive-clock time of the change */
    occ_state_t previous_state;
    occ_state_t new_state;
    occ_reason_t reason;
    uint16_t motion_streak;   /**< MOTION windows in a row (entry) */
    int64_t evidence_age_us;  /**< time since last evidence (exit) */
    occ_evidence_t last_evidence; /**< source of the last evidence (exit) */
} occ_event_t;

/* Result of one call: at most one closed window and one state change. */
typedef struct {
    bool has_stats; /**< a window closed and `stats` is valid */
    occ_stats_t stats;
    bool has_event; /**< the occupancy state changed and `event` is valid */
    occ_event_t event;
} occ_output_t;

/* Per-subcarrier running sums for the current window. */
typedef struct {
    double sum;            /**< sum x */
    double sum_squares;    /**< sum x^2 */
    double pair_products;  /**< sum over pairs x_t * x_{t+1} */
    double pair_first;     /**< sum over pairs x_t */
    double pair_second;    /**< sum over pairs x_{t+1} */
    double previous;       /**< x of the previous packet */
} occ_subcarrier_sums_t;

/* Core state. Callers allocate it (static or stack); size is fixed. */
typedef struct {
    occ_config_t config;

    bool window_open;
    uint32_t window_index;
    uint32_t first_seq;
    uint32_t last_seq;
    int64_t first_rx_time_us;
    int64_t last_rx_time_us;
    uint16_t packets;
    uint16_t pairs;
    uint16_t duplicates;
    uint16_t dropped_since_window; /**< repeats / late packets outside a window */
    bool stream_started_in_window; /**< first window after start or reset */
    uint16_t required_csi_len;     /**< bytes needed for the highest index */

    /* Stream and gap tracking. */
    bool stream_started;
    int64_t last_packet_time_us;
    bool has_emitted_window;
    uint32_t last_emitted_window_index;
    uint32_t pending_empty_windows;  /**< fully lost windows not yet reported */
    int64_t pending_detected_us;

    /* Receive clock seen through push / advance_time (reset detection). */
    bool clock_started;
    int64_t clock_us;

    double false_alarm_z;          /**< upper normal quantile of false_alarm_rate */

    /* State machine. */
    occ_state_t state;
    uint16_t motion_streak;
    bool has_previous_decision;
    uint32_t previous_window_index;
    int64_t last_evidence_us;
    occ_evidence_t last_evidence;

    occ_subcarrier_sums_t sums[OCC_MAX_SUBCARRIERS];
} occ_core_t;

/* Defaults from spec 0001 and report 0001: link 0, 100 packets per window,
 * 80 packets minimum, pair indices 0-56 and 60-116 (114 subcarriers, DC and
 * its two neighbours excluded, pilots included), per-packet gain
 * normalization, false-alarm rate 1e-3 per
 * window, F_eff 60, empty mean -1/n, entry after 2 MOTION windows, hold
 * time 600 s. F_eff and the false-alarm rate are recalibrated in ticket 05. */
void occ_default_config(occ_config_t *config);

/* Returns false if the configuration is invalid (zero window, index out of
 * range, too many subcarriers, false-alarm rate outside (0, 0.5), F_eff or
 * entry windows not positive, negative hold time). */
bool occ_core_init(occ_core_t *core, const occ_config_t *config);

/* Feed one record in arrival order. When the record starts a new window the
 * previous window is closed and returned in `output`. Records whose csi_len
 * does not cover every configured subcarrier, or (normalization) whose
 * configured subcarriers are all zero, are rejected (returns false).
 *
 * Seq rules: an exact repeat of the previous seq is a duplicate and dropped;
 * any other backward step is a transmitter restart (new stream, no pairs or
 * gap windows across it). If whole windows were skipped, call occ_core_poll
 * until it returns false to receive one empty (undecidable) window each. */
bool occ_core_push(occ_core_t *core, const occ_csi_record_t *record, occ_output_t *output);

/* Close the open window (end of a replay). Returns false if none was open. */
bool occ_core_flush(occ_core_t *core, occ_output_t *output);

/* Report the next fully lost window found by the last push. Returns false
 * when none is pending. */
bool occ_core_poll(occ_core_t *core, occ_output_t *output);

/* Advance the receive clock without a record. Call it before every push with
 * the record's time and periodically while idle (device), and repeat while
 * it produces output. After `window_duration_us` without packets it closes
 * the open window (later packets for that window index are dropped and
 * counted, so an index is never reported twice). It never invents windows:
 * occ_core_poll reports fully lost windows once packets resume. It checks
 * the hold time so OCCUPIED expires during silence; an exit event carries
 * its scheduled time (last evidence + hold time). Times earlier than the
 * last record are ignored; a device should pass a clock estimate that does
 * not run ahead of the record clock. */
void occ_core_advance_time(occ_core_t *core, int64_t now_us, occ_output_t *output);

/* Put the state machine in OCCUPIED with evidence at `time_us`, without an
 * event. For starting from a known occupied room (evaluation of seated or
 * lying sessions, or a device restoring its last state). */
void occ_core_set_occupied(occ_core_t *core, int64_t time_us);

/* Register occupancy evidence other than motion (stage 2). In OCCUPIED it
 * restarts the hold time; in EMPTY it is ignored (entry needs motion). */
void occ_core_add_evidence(occ_core_t *core, int64_t time_us, occ_evidence_t source);

/* Format a stats record as one text line without newline:
 *   OCC_STATS,<link>,<window>,<first_us>,<last_us>,<motion>,<packets>,
 *   <pairs>,<lost>,<duplicates>,<subcarriers>,<status>,<eta>,<decision>
 * Returns the snprintf result. Firmware and PC harness share this so their
 * output can be compared line by line. */
int occ_format_stats(char *buffer, size_t size, const occ_stats_t *stats);

/* Format an event as one text line without newline:
 *   OCC_EVENT,<link>,<window>,<time_us>,<previous_state>,<new_state>,
 *   <reason>,<motion_streak>,<evidence_age_us>,<last_evidence> */
int occ_format_event(char *buffer, size_t size, const occ_event_t *event);

#ifdef __cplusplus
}
#endif
