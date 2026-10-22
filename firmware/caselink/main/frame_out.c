#include "frame_out.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define FRAME_UART UART_NUM_0
#define FRAME_UART_TX_BUFFER 16384
#define FRAME_UART_RX_BUFFER 1024
#define FRAME_HEADER_SIZE 5
#define FRAME_TEXT_MAX 240

static SemaphoreHandle_t s_write_lock;

static uint16_t crc16_ccitt(uint16_t crc, const uint8_t *data, int length) {
    for (int index = 0; index < length; index++) {
        crc ^= (uint16_t)data[index] << 8;

        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x8000) {
                crc = (uint16_t)((crc << 1) ^ 0x1021);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }

    return crc;
}

/* ESP_LOG lines become text frames instead of raw console bytes. */
static int log_to_frame(const char *format, va_list args) {
    char text[FRAME_TEXT_MAX];
    int length = vsnprintf(text, sizeof(text), format, args);

    if (length <= 0) {
        return length;
    }

    if (length >= (int)sizeof(text)) {
        length = sizeof(text) - 1;
    }

    while (length > 0 &&
           (text[length - 1] == '\n' || text[length - 1] == '\r')) {
        length--;
    }

    frame_out_write(FRAME_TYPE_TEXT, text, (uint16_t)length);
    return length;
}

void frame_out_init(void) {
    s_write_lock = xSemaphoreCreateMutex();

    ESP_ERROR_CHECK(uart_driver_install(FRAME_UART, FRAME_UART_RX_BUFFER,
                                        FRAME_UART_TX_BUFFER, 0, NULL, 0));

    esp_log_set_vprintf(log_to_frame);
}

void frame_out_write(uint8_t type, const void *payload, uint16_t length) {
    if (length > FRAME_MAX_PAYLOAD) {
        length = FRAME_MAX_PAYLOAD;
    }

    uint8_t header[FRAME_HEADER_SIZE] = {
        0xA5, 0x5A, type, (uint8_t)(length & 0xFF), (uint8_t)(length >> 8),
    };

    uint16_t crc = crc16_ccitt(0xFFFF, &header[2], 3);
    crc = crc16_ccitt(crc, payload, length);

    uint8_t trailer[2] = {
        (uint8_t)(crc & 0xFF),
        (uint8_t)(crc >> 8),
    };

    xSemaphoreTake(s_write_lock, portMAX_DELAY);
    uart_write_bytes(FRAME_UART, header, sizeof(header));
    uart_write_bytes(FRAME_UART, payload, length);
    uart_write_bytes(FRAME_UART, trailer, sizeof(trailer));
    xSemaphoreGive(s_write_lock);
}

void frame_out_text(const char *format, ...) {
    char text[FRAME_TEXT_MAX];

    va_list args;
    va_start(args, format);
    int length = vsnprintf(text, sizeof(text), format, args);
    va_end(args);

    if (length < 0) {
        return;
    }

    if (length >= (int)sizeof(text)) {
        length = sizeof(text) - 1;
    }

    frame_out_write(FRAME_TYPE_TEXT, text, (uint16_t)length);
}

int frame_out_read_line(char *line, int line_size) {
    int length = 0;

    while (true) {
        uint8_t byte = 0;
        int got = uart_read_bytes(FRAME_UART, &byte, 1, portMAX_DELAY);

        if (got != 1) {
            continue;
        }

        if (byte == '\r') {
            continue;
        }

        if (byte == '\n') {
            line[length] = '\0';
            return length;
        }

        if (length < line_size - 1) {
            line[length] = (char)byte;
            length++;
        }
    }
}
