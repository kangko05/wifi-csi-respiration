/*
 * csi-link: one firmware for both boards of a submodule.
 *
 * The role (tx / rx / idle) and every radio setting come from NVS and are
 * changed over UART0 with text commands, so a time-limited test session never
 * needs a rebuild or reflash:
 *
 *   get                 print the stored config
 *   set <key> <value>   change one field (role link ch bw period power rate
 *                       gain lltf agc fft relay)
 *   apply               save to NVS and restart with the new config
 *   defaults            reset the stored config to defaults (needs apply)
 *   status              print counters now (also printed every second)
 *   restart             restart without saving
 *
 * Every reply starts with "ok" or "err" and arrives as a text frame.
 */

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "frame_out.h"
#include "link_config.h"
#include "radio.h"

#define FIRMWARE_NAME "csi-link"
#define FIRMWARE_VERSION 1
#define STATUS_PERIOD_MS 1000
#define COMMAND_LINE_MAX 96
#define CONFIG_TEXT_MAX 192

/* Config being edited by commands; the running radio keeps the boot copy. */
static link_config_t s_pending_config;
/* Commands and the one-time baseline save both write s_pending_config. */
static SemaphoreHandle_t s_pending_lock;

static void nvs_init(void) {
    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }

    ESP_ERROR_CHECK(err);
}

static void print_config(const char *prefix, const link_config_t *config) {
    char text[CONFIG_TEXT_MAX];
    link_config_format(config, text, sizeof(text));
    frame_out_text("%s %s", prefix, text);
}

static void handle_command(char *line) {
    char *command = strtok(line, " ");

    if (command == NULL) {
        return;
    }

    if (strcmp(command, "get") == 0) {
        print_config("ok", &s_pending_config);
        return;
    }

    if (strcmp(command, "set") == 0) {
        char *key = strtok(NULL, " ");
        char *value = strtok(NULL, " ");

        if (key == NULL || value == NULL) {
            frame_out_text("err usage: set <key> <value>");
            return;
        }

        esp_err_t err = link_config_set(&s_pending_config, key, value);

        if (err == ESP_ERR_NOT_FOUND) {
            frame_out_text("err unknown key %s", key);
            return;
        }

        if (err != ESP_OK) {
            frame_out_text("err bad value %s=%s", key, value);
            return;
        }

        print_config("ok", &s_pending_config);
        return;
    }

    if (strcmp(command, "defaults") == 0) {
        link_config_defaults(&s_pending_config);
        print_config("ok", &s_pending_config);
        return;
    }

    if (strcmp(command, "apply") == 0) {
        esp_err_t err = link_config_save(&s_pending_config);

        if (err != ESP_OK) {
            frame_out_text("err save %s", esp_err_to_name(err));
            return;
        }

        frame_out_text("ok restarting");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    }

    if (strcmp(command, "restart") == 0) {
        frame_out_text("ok restarting");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    }

    if (strcmp(command, "status") == 0) {
        char text[CONFIG_TEXT_MAX];
        radio_format_status(text, sizeof(text));
        frame_out_text("ok %s", text);
        return;
    }

    frame_out_text("err unknown command %s", command);
}

static void command_task(void *argument) {
    (void)argument;

    char line[COMMAND_LINE_MAX];

    while (true) {
        frame_out_read_line(line, sizeof(line));

        xSemaphoreTake(s_pending_lock, portMAX_DELAY);
        handle_command(line);
        xSemaphoreGive(s_pending_lock);
    }
}

/* Stores the gain measured this boot so later boots force the same pair.
 * The NVS copy is reloaded rather than taken from s_pending_config, which may
 * hold edits the user has not applied. */
static void save_measured_gain(uint8_t agc_gain, int8_t fft_gain) {
    link_config_t stored_config;
    link_config_load(&stored_config);

    stored_config.forced_agc = agc_gain;
    stored_config.forced_fft = fft_gain;
    esp_err_t err = link_config_save(&stored_config);

    if (err != ESP_OK) {
        frame_out_text("err gain save %s", esp_err_to_name(err));
        return;
    }

    xSemaphoreTake(s_pending_lock, portMAX_DELAY);

    /* A pair the user already set but has not applied wins. */
    if (s_pending_config.forced_agc == 0 && s_pending_config.forced_fft == 0) {
        s_pending_config.forced_agc = agc_gain;
        s_pending_config.forced_fft = fft_gain;
    }

    xSemaphoreGive(s_pending_lock);

    frame_out_text("gain saved agc=%u fft=%d", agc_gain, fft_gain);
}

/* Also owns the one-time gain save, because NVS writes must stay out of the
 * Wi-Fi task where the baseline is measured. */
static void status_task(void *argument) {
    (void)argument;

    char text[CONFIG_TEXT_MAX];
    bool gain_saved = false;

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(STATUS_PERIOD_MS));

        uint8_t agc_gain = 0;
        int8_t fft_gain = 0;

        if (!gain_saved &&
            radio_get_measured_forced_gain(&agc_gain, &fft_gain)) {
            save_measured_gain(agc_gain, fft_gain);
            gain_saved = true;
        }

        radio_format_status(text, sizeof(text));
        frame_out_write(FRAME_TYPE_STATUS, text, strlen(text));
    }
}

void app_main(void) {
    nvs_init();
    frame_out_init();

    link_config_t boot_config;
    esp_err_t err = link_config_load(&boot_config);

    if (err != ESP_OK) {
        frame_out_text("config not loaded (%s), using defaults",
                       esp_err_to_name(err));
    }

    s_pending_config = boot_config;
    s_pending_lock = xSemaphoreCreateMutex();

    uint8_t boot_id = 0;
    err = link_boot_id_next(&boot_id);

    if (err != ESP_OK) {
        frame_out_text("boot id not stored (%s)", esp_err_to_name(err));
    }

    frame_out_text("boot %s v%d boot_id=%u", FIRMWARE_NAME, FIRMWARE_VERSION,
                   boot_id);
    print_config("config", &boot_config);

    xTaskCreate(command_task, "command", 4096, NULL, 5, NULL);

    radio_start(&boot_config, boot_id);

    xTaskCreate(status_task, "status", 3072, NULL, 4, NULL);
}
