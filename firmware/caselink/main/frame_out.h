#pragma once

#include <stdint.h>

/*
 * Everything the board says goes out on UART0 as frames, so binary CSI and
 * text never get mixed up on the host:
 *
 *   0xA5 0x5A | type u8 | length u16 LE | payload | crc16 u16 LE
 *
 * crc16 is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over type, length and
 * payload. Boot ROM output and panic dumps stay plain text between frames; the
 * host resynchronises on the magic.
 */

#define FRAME_TYPE_TEXT 1
#define FRAME_TYPE_CSI 2
#define FRAME_TYPE_STATUS 3

#define FRAME_MAX_PAYLOAD 1024

void frame_out_init(void);

/* Thread-safe; blocks until the bytes are queued in the UART driver. */
void frame_out_write(uint8_t type, const void *payload, uint16_t length);

/* printf-style text frame. */
void frame_out_text(const char *format, ...) __attribute__((format(printf, 1, 2)));

/* Reads one command line from UART0 (without the newline). Blocks. */
int frame_out_read_line(char *line, int line_size);
