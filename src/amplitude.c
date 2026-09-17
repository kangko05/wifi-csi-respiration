#include "csi_resp/amplitude.h"
#include "csi_resp/csi_utils.h"

/* frames[0] 기준 시각 [us]. uint32 뺄셈이라 창 안의 wrap 1회는 자동 처리된다.
 */
static uint32_t rel_time(const csi_frame_t *frames, size_t i) {
    return frames[i].meta.timestamp - frames[0].meta.timestamp;
}

static float frame_amp(const csi_frame_t *f, uint16_t k) {
    return csi_abs(f->re[k], f->im[k]);
}

/* i부터 시작해 칸 k가 유효한 첫 프레임 인덱스. 없으면 n. */
static size_t next_valid(const csi_frame_t *frames, size_t n, uint16_t k,
                         size_t i) {
    while (i < n && !frames[i].valid[k]) {
        i++;
    }

    return i;
}

/* 칸 k가 보간 조건을 통과하는지 검사하고, 첫/마지막 유효 시각을 돌려준다. */
static int bin_eligible(const csi_frame_t *frames, size_t n, uint16_t k,
                        uint32_t *first, uint32_t *last) {
    size_t count = 0;
    uint32_t prev = 0; /* 창 시작 */
    uint32_t max_gap = 0;

    for (size_t i = 0; i < n; i++) {
        if (!frames[i].valid[k]) {
            continue;
        }

        uint32_t t = rel_time(frames, i);

        if (count == 0) {
            *first = t;
        }

        if (t - prev > max_gap) {
            max_gap = t - prev;
        }

        prev = t;

        count++;
    }

    if (count == 0) {
        return 0;
    }

    uint32_t end = rel_time(frames, n - 1); /* 창 끝 */

    if (end - prev > max_gap) {
        max_gap = end - prev;
    }

    *last = prev;

    return count >= CSI_INTERP_MIN_PACKETS &&
           (float)count / (float)n >= CSI_INTERP_MIN_FRACTION &&
           max_gap <= CSI_INTERP_MAX_GAP_US;
}

size_t csi_amp_interp(const csi_frame_t *frames, size_t n_frames, float *out,
                      uint8_t *eligible, size_t max_grid, uint32_t *start_us) {
    if (n_frames < 2) {
        return 0;
    }

    uint16_t n_bins = frames[0].n;

    for (size_t i = 1; i < n_frames; i++) {
        if (frames[i].n != n_bins ||
            rel_time(frames, i) < rel_time(frames, i - 1)) {
            return 0; /* 칸 수가 다르거나 시각이 역전됨 */
        }
    }

    /* 칸 선택 + 격자 범위 */
    uint32_t start = 0;
    uint32_t end = UINT32_MAX;
    uint16_t n_eligible = 0;

    for (uint16_t k = 0; k < n_bins; k++) {
        uint32_t first = 0;
        uint32_t last = 0;
        eligible[k] = (uint8_t)bin_eligible(frames, n_frames, k, &first, &last);

        if (!eligible[k]) {
            continue;
        }

        n_eligible++;

        if (first > start) {
            start = first;
        }

        if (last < end) {
            end = last;
        }
    }

    if (n_eligible == 0 || end < start) {
        return 0;
    }

    size_t n_grid = (end - start) / CSI_INTERP_STEP_US + 1;

    if (n_grid > max_grid) {
        return 0;
    }

    /* 칸마다 선형 보간. lo = 시각 <= x인 마지막 유효 프레임, hi = 그다음 유효
     * 프레임 */
    for (uint16_t k = 0; k < n_bins; k++) {
        if (!eligible[k]) {
            for (size_t g = 0; g < n_grid; g++) {
                out[g * n_bins + k] = 0.0f;
            }
            continue;
        }

        size_t lo = next_valid(frames, n_frames, k, 0);
        size_t hi = next_valid(frames, n_frames, k, lo + 1);

        for (size_t g = 0; g < n_grid; g++) {
            uint32_t x = start + (uint32_t)(g * CSI_INTERP_STEP_US);

            while (hi < n_frames && rel_time(frames, hi) <= x) {
                lo = hi;
                hi = next_valid(frames, n_frames, k, hi + 1);
            }

            uint32_t t_lo = rel_time(frames, lo);
            float a_lo = frame_amp(&frames[lo], k);

            if (hi >= n_frames || t_lo == x) {
                out[g * n_bins + k] = a_lo;
            } else {
                uint32_t t_hi = rel_time(frames, hi);
                float a_hi = frame_amp(&frames[hi], k);
                float frac = (float)(x - t_lo) / (float)(t_hi - t_lo);
                out[g * n_bins + k] = a_lo + frac * (a_hi - a_lo);
            }
        }
    }

    *start_us = start;
    return n_grid;
}
