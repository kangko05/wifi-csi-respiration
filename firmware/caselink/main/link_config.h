#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef enum {
    LINK_ROLE_IDLE = 0,
    LINK_ROLE_TX = 1,
    LINK_ROLE_RX = 2,
} link_role_t;

/*
 * Everything a test session may change without rebuilding. Stored in NVS so a
 * board boots into the same setup when it is powered without the laptop.
 * Radio fields take effect only after a restart (command "apply").
 */
typedef struct {
    uint8_t role;             /**< link_role_t */
    uint8_t link_id;          /**< last byte of the transmitter MAC, separates submodules */
    uint8_t channel;          /**< 5 GHz primary channel */
    uint8_t bandwidth_mhz;    /**< 20 or 40; 40 uses the secondary channel above */
    uint16_t period_ms;       /**< transmit period */
    int8_t tx_power_qdbm;     /**< max TX power in 0.25 dBm, 0 keeps the PHY default */
    uint8_t phy_rate;         /**< wifi_phy_rate_t used by the transmitter */
    uint8_t force_gain;       /**< receiver: 1 fixes AGC/FFT gain to the measured baseline */
    uint8_t acquire_lltf;     /**< receiver: 1 also asks for legacy LTF CSI */
    uint8_t forced_agc;       /**< receiver: AGC gain forced with gain=force */
    int8_t forced_fft;        /**< receiver: FFT gain forced with gain=force;
                                   (0, 0) means not set, measure at boot */
    uint8_t relay;            /**< receiver: 1 also sends each CSI record as an
                                   ARI2 packet to the main unit over ESP-NOW,
                                   LINK_RELAY_AP as UDP through the AP */
} link_config_t;

/* relay value: join the main module's AP and send each ARI2 packet as one UDP
 * datagram to CONFIG_LINK_AP_HOST:CONFIG_LINK_AP_PORT. ch must be the AP's. */
#define LINK_RELAY_AP 3

void link_config_defaults(link_config_t *config);

esp_err_t link_config_load(link_config_t *config);

esp_err_t link_config_save(const link_config_t *config);

/* Sets one field from text. Returns ESP_ERR_NOT_FOUND for an unknown key and
 * ESP_ERR_INVALID_ARG for an out-of-range value. */
esp_err_t link_config_set(link_config_t *config, const char *key, const char *value);

/* Writes "key=value ..." into text (always NUL-terminated). */
void link_config_format(const link_config_t *config, char *text, int text_size);

const char *link_role_name(uint8_t role);

/* Increments the stored boot counter (wraps at 256) and returns the new value,
 * so records from different boots can be told apart. */
esp_err_t link_boot_id_next(uint8_t *boot_id);
