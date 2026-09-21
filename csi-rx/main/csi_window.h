#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "csi_resp/csi_record.h"

/* Owned exclusively by the processing task. Records stay in raw int8 form. */
typedef struct {
    csi_record_t *records;
    size_t capacity;
    size_t head;
    size_t count;
    uint32_t duration_us;
    uint32_t capacity_drops;
} csi_window_t;

bool csi_window_init(csi_window_t *window, csi_record_t *storage,
                     size_t capacity, uint32_t duration_us);
/* Keep the half-open interval (now - duration, now]. Unsigned subtraction
 * handles timestamp wrap. Call regularly; timestamps must move forward. */
void csi_window_expire(csi_window_t *window, uint32_t now);
/* Returns false for malformed records or timestamps moving backwards.
 * If capacity is exhausted, evicts the oldest and increments capacity_drops. */
bool csi_window_push(csi_window_t *window, const csi_record_t *record);
/* Chronological access, index 0 is oldest. Pointer valid until next mutation. */
const csi_record_t *csi_window_at(const csi_window_t *window, size_t index);
