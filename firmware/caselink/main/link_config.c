#include "link_config.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_wifi_types.h"
#include "nvs.h"

#define NVS_NAMESPACE "csilink"
#define NVS_KEY "config"
#define NVS_BOOT_ID_KEY "boot_id"

/* Bumped whenever link_config_t changes layout; older layouts are migrated
 * in link_config_load. */
#define CONFIG_BLOB_VERSION 3

typedef struct {
    uint8_t version;
    link_config_t config;
} config_blob_t;

/* link_config_t as stored by blob version 2, before relay. Version 3 only
 * appends, so a v2 blob is the prefix of the current layout. */
typedef struct {
    uint8_t role;
    uint8_t link_id;
    uint8_t channel;
    uint8_t bandwidth_mhz;
    uint16_t period_ms;
    int8_t tx_power_qdbm;
    uint8_t phy_rate;
    uint8_t force_gain;
    uint8_t acquire_lltf;
    uint8_t forced_agc;
    int8_t forced_fft;
} link_config_v2_t;

_Static_assert(offsetof(link_config_t, relay) == sizeof(link_config_v2_t),
               "v2 fields must stay a prefix of link_config_t");

typedef struct {
    uint8_t version;
    link_config_v2_t config;
} config_blob_v2_t;

/* link_config_t as stored by blob version 1, before forced_agc/forced_fft. */
typedef struct {
    uint8_t role;
    uint8_t link_id;
    uint8_t channel;
    uint8_t bandwidth_mhz;
    uint16_t period_ms;
    int8_t tx_power_qdbm;
    uint8_t phy_rate;
    uint8_t force_gain;
    uint8_t acquire_lltf;
} link_config_v1_t;

typedef struct {
    uint8_t version;
    link_config_v1_t config;
} config_blob_v1_t;

typedef union {
    uint8_t version;
    config_blob_t current;
    config_blob_v2_t v2;
    config_blob_v1_t v1;
} stored_blob_t;

void link_config_defaults(link_config_t *config) {
    memset(config, 0, sizeof(*config));

    config->role = LINK_ROLE_IDLE;
    config->link_id = 1;
    config->channel = 36;
    config->bandwidth_mhz = 20;
    config->period_ms = 10;
    config->tx_power_qdbm = 0;
    config->phy_rate = WIFI_PHY_RATE_MCS0_LGI;
    config->force_gain = 0;
    config->acquire_lltf = 0;
    config->forced_agc = 0;
    config->forced_fft = 0;
    config->relay = 0;
}

static void migrate_v1(const link_config_v1_t *old_config, link_config_t *config) {
    config->role = old_config->role;
    config->link_id = old_config->link_id;
    config->channel = old_config->channel;
    config->bandwidth_mhz = old_config->bandwidth_mhz;
    config->period_ms = old_config->period_ms;
    config->tx_power_qdbm = old_config->tx_power_qdbm;
    config->phy_rate = old_config->phy_rate;
    config->force_gain = old_config->force_gain;
    config->acquire_lltf = old_config->acquire_lltf;
    config->forced_agc = 0;
    config->forced_fft = 0;
    config->relay = 0;
}

/* Fields the caller already set to defaults (relay) keep their value. */
static void migrate_v2(const link_config_v2_t *old_config, link_config_t *config) {
    memcpy(config, old_config, sizeof(*old_config));
}

esp_err_t link_config_load(link_config_t *config) {
    link_config_defaults(config);

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);

    if (err != ESP_OK) {
        return err;
    }

    /* nvs_get_blob accepts a buffer larger than the stored blob and reports
     * the stored size, so one read covers every version. */
    stored_blob_t blob;
    size_t blob_size = sizeof(blob);
    err = nvs_get_blob(handle, NVS_KEY, &blob, &blob_size);
    nvs_close(handle);

    if (err != ESP_OK) {
        return err;
    }

    if (blob_size == sizeof(blob.current) && blob.version == CONFIG_BLOB_VERSION) {
        *config = blob.current.config;
        return ESP_OK;
    }

    if (blob_size == sizeof(blob.v2) && blob.version == 2) {
        migrate_v2(&blob.v2.config, config);
        return ESP_OK;
    }

    if (blob_size == sizeof(blob.v1) && blob.version == 1) {
        migrate_v1(&blob.v1.config, config);
        return ESP_OK;
    }

    return ESP_ERR_INVALID_VERSION;
}

esp_err_t link_config_save(const link_config_t *config) {
    config_blob_t blob = {
        .version = CONFIG_BLOB_VERSION,
        .config = *config,
    };

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);

    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_blob(handle, NVS_KEY, &blob, sizeof(blob));

    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }

    nvs_close(handle);
    return err;
}

esp_err_t link_boot_id_next(uint8_t *boot_id) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);

    if (err != ESP_OK) {
        return err;
    }

    uint8_t previous_boot_id = 0;
    err = nvs_get_u8(handle, NVS_BOOT_ID_KEY, &previous_boot_id);

    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        return err;
    }

    *boot_id = (uint8_t)(previous_boot_id + 1);
    err = nvs_set_u8(handle, NVS_BOOT_ID_KEY, *boot_id);

    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }

    nvs_close(handle);
    return err;
}

static bool parse_long(const char *text, long min_value, long max_value,
                       long *out) {
    char *end = NULL;
    long value = strtol(text, &end, 0);

    if (end == text || *end != '\0') {
        return false;
    }

    if (value < min_value || value > max_value) {
        return false;
    }

    *out = value;
    return true;
}

static esp_err_t parse_role(const char *value, uint8_t *role) {
    if (strcmp(value, "idle") == 0) {
        *role = LINK_ROLE_IDLE;
        return ESP_OK;
    }

    if (strcmp(value, "tx") == 0) {
        *role = LINK_ROLE_TX;
        return ESP_OK;
    }

    if (strcmp(value, "rx") == 0) {
        *role = LINK_ROLE_RX;
        return ESP_OK;
    }

    return ESP_ERR_INVALID_ARG;
}

esp_err_t link_config_set(link_config_t *config, const char *key,
                          const char *value) {
    long number = 0;

    if (strcmp(key, "role") == 0) {
        return parse_role(value, &config->role);
    }

    if (strcmp(key, "link") == 0) {
        /* 0 is allowed: the main unit (csi-main) maps link 0..2 to room 1..3. */
        if (!parse_long(value, 0, 254, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->link_id = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "ch") == 0) {
        if (!parse_long(value, 36, 177, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->channel = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "bw") == 0) {
        if (!parse_long(value, 20, 40, &number) ||
            (number != 20 && number != 40)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->bandwidth_mhz = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "period") == 0) {
        if (!parse_long(value, 2, 1000, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->period_ms = (uint16_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "power") == 0) {
        /* esp_wifi_set_max_tx_power accepts 8..84 (2..21 dBm); 0 means default.
         */
        if (!parse_long(value, 0, 84, &number) || (number != 0 && number < 8)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->tx_power_qdbm = (int8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "rate") == 0) {
        if (!parse_long(value, 0, 255, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->phy_rate = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "gain") == 0) {
        if (strcmp(value, "auto") == 0) {
            config->force_gain = 0;
            return ESP_OK;
        }
        if (strcmp(value, "force") == 0) {
            config->force_gain = 1;
            return ESP_OK;
        }
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(key, "lltf") == 0) {
        if (!parse_long(value, 0, 1, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->acquire_lltf = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "agc") == 0) {
        if (!parse_long(value, 0, UINT8_MAX, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->forced_agc = (uint8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "fft") == 0) {
        if (!parse_long(value, INT8_MIN, INT8_MAX, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->forced_fft = (int8_t)number;
        return ESP_OK;
    }

    if (strcmp(key, "relay") == 0) {
        /* 0 off, 1 unicast to the main unit, 2 broadcast (no ACK needed). */
        if (!parse_long(value, 0, 2, &number)) {
            return ESP_ERR_INVALID_ARG;
        }
        config->relay = (uint8_t)number;
        return ESP_OK;
    }

    return ESP_ERR_NOT_FOUND;
}

const char *link_role_name(uint8_t role) {
    if (role == LINK_ROLE_TX) {
        return "tx";
    }

    if (role == LINK_ROLE_RX) {
        return "rx";
    }

    return "idle";
}

void link_config_format(const link_config_t *config, char *text,
                        int text_size) {
    snprintf(text, text_size,
             "role=%s link=%u ch=%u bw=%u period=%u power=%d rate=%u gain=%s "
             "lltf=%u agc=%u fft=%d relay=%u",
             link_role_name(config->role), config->link_id, config->channel,
             config->bandwidth_mhz, config->period_ms, config->tx_power_qdbm,
             config->phy_rate, config->force_gain ? "force" : "auto",
             config->acquire_lltf, config->forced_agc, config->forced_fft,
             config->relay);
}
