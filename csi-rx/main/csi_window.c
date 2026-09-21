#include "csi_window.h"

bool csi_window_init(csi_window_t *window, csi_record_t *storage,
                     size_t capacity, uint32_t duration_us) {
    if (!window || !storage || !capacity || !duration_us ||
        duration_us >= UINT32_C(0x80000000)) {
        return false;
    }

    *window = (csi_window_t){
        .records = storage, .capacity = capacity, .duration_us = duration_us};

    return true;
}

const csi_record_t *csi_window_at(const csi_window_t *window, size_t index) {
    if (!window || index >= window->count) {
        return NULL;
    }

    return &window->records[(window->head + index) % window->capacity];
}

void csi_window_expire(csi_window_t *window, uint32_t now) {
    while (window->count) {
        const csi_record_t *oldest = csi_window_at(window, 0);

        if ((uint32_t)(now - oldest->timestamp) < window->duration_us) {
            break;
        }

        window->head = (window->head + 1) % window->capacity;
        --window->count;
    }
}

bool csi_window_push(csi_window_t *window, const csi_record_t *record) {
    if (!window || !window->records || !window->capacity || !record ||
        !record->len || record->len > CSI_BUF_MAX || (record->len % 2) != 0) {
        return false;
    }

    if (window->count) {
        const csi_record_t *last = csi_window_at(window, window->count - 1);

        if ((uint32_t)(record->timestamp - last->timestamp) >=
            UINT32_C(0x80000000)) {
            return false;
        }
    }
    csi_window_expire(window, record->timestamp);

    if (window->count == window->capacity) {
        window->head = (window->head + 1) % window->capacity;
        --window->count;
        ++window->capacity_drops;
    }

    window->records[(window->head + window->count) % window->capacity] =
        *record;

    ++window->count;

    return true;
}
