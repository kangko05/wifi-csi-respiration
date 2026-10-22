#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "link_config.h"

/*
 * CSI frame payload (FRAME_TYPE_CSI), little-endian, packed:
 *
 *   u32 rx_count      accepted CSI callbacks since boot (gaps = queue drops)
 *   u32 timestamp_us  rx_ctrl.timestamp, receiver clock, wraps at 2^32
 *   u16 rx_seq        802.11 sequence number of the frame
 *   i8  rssi          dBm
 *   i8  noise_floor   dBm
 *   u8  agc_gain
 *   i8  fft_gain
 *   u8  channel       primary channel
 *   u8  second        secondary channel field of rx_ctrl
 *   u8  bb_format     rx_ctrl.cur_bb_format
 *   u8  rate
 *   u8  flags         bit0 first_word_invalid, bit1 gain forced,
 *                     bit2 rx_channel_estimate_info_vld,
 *                     bit3 compensate_gain valid
 *   u8  boot_id       NVS boot counter, wraps at 256; rx_count restarts
 *                     at 0 on every boot
 *   u16 csi_length    CSI bytes that follow the header
 *   i8  csi[csi_length]  interleaved (imag, real) pairs as delivered by the driver
 *   f32 compensate_gain  trailer after the CSI bytes: esp_csi_gain_ctrl factor
 *                     for this packet's (agc, fft) gain against the boot
 *                     baseline. Reported only, never applied to csi. Older
 *                     decoders read csi_length bytes and ignore it.
 */
typedef struct __attribute__((packed)) {
    uint32_t rx_count;
    uint32_t timestamp_us;
    uint16_t rx_seq;
    int8_t rssi;
    int8_t noise_floor;
    uint8_t agc_gain;
    int8_t fft_gain;
    uint8_t channel;
    uint8_t second;
    uint8_t bb_format;
    uint8_t rate;
    uint8_t flags;
    uint8_t boot_id;
    uint16_t csi_length;
} csi_record_header_t;

#define CSI_FLAG_FIRST_WORD_INVALID 0x01
#define CSI_FLAG_GAIN_FORCED 0x02
#define CSI_FLAG_ESTIMATE_VALID 0x04
#define CSI_FLAG_COMPENSATE_VALID 0x08

/* Starts Wi-Fi and the role's tasks. Does nothing for LINK_ROLE_IDLE. */
void radio_start(const link_config_t *config, uint8_t boot_id);

/* True once this boot measured and forced a gain baseline because gain=force
 * had no stored (forced_agc, forced_fft); the caller persists it. False when
 * the stored pair was used, with gain=auto, and before the baseline is ready. */
bool radio_get_measured_forced_gain(uint8_t *agc_gain, int8_t *fft_gain);

/* One-line counters for the status frame. */
void radio_format_status(char *text, int text_size);
