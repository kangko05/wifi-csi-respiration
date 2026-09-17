#include <stdio.h>

#include "csi_resp/preprocess.h"

/*
 * 예시 입력: 실제 수집 데이터의 CSI 한 줄을 csi_record_t로 옮긴 것.
 * 출처: data/20260916T054244_118183_c80cd924/serial.bin, lines.csv line_index 1
 *   CSI_DATA,346520,-29,-94,17,16,36,3137449245,47,2,234,0,0.365...
 * buf는 base64 디코딩한 int8 값 그대로 ([im, real] 쌍, 234바이트).
 */
static const csi_record_t example_rec = {
    .seq = 346520,
    .rssi = -29,
    .noise_floor = -94,
    .fft_gain = 17,
    .agc_gain = 16,
    .channel = 36,
    .timestamp = 3137449245u,
    .sig_len = 47,
    .bb_format = 2,
    .len = 234,
    .first_word_invalid = 0,
    .compensate_gain = 0.365174f,
    .dropped = 0,
    .buf =
        {
            -4,  -15, -1,  -17, -3,  -18, -3,  -20, -3,  -19, -2,  -20, -1,
            -20, -1,  -21, 1,   -23, -2,  -22, 1,   -22, 0,   -24, -2,  -25,
            -1,  -24, 2,   -23, -1,  -24, -1,  -26, 1,   -24, -2,  -23, 1,
            -24, 1,   -25, 1,   -23, 1,   -23, 0,   -24, 1,   -25, -3,  -23,
            -3,  -26, -1,  -27, -3,  -24, -5,  -25, -3,  -24, -3,  -28, -3,
            -25, -3,  -27, -4,  -25, -6,  -26, -6,  -25, -7,  -28, -5,  -26,
            -9,  -26, -8,  -27, -9,  -24, -9,  -25, -10, -25, -10, -22, -11,
            -23, -12, -23, -12, -21, -13, -22, -13, -20, -15, -19, -16, -17,
            -16, -19, -17, -15, -19, -16, -21, -16, -20, -14, 0,   0,   0,
            0,   0,   0,   -12, 24,  -9,  24,  -7,  25,  -8,  27,  -5,  27,
            -5,  29,  -4,  29,  -3,  29,  -2,  28,  -3,  28,  -1,  29,  0,
            29,  0,   31,  2,   29,  3,   29,  3,   28,  4,   31,  5,   30,
            5,   31,  6,   28,  6,   28,  6,   29,  8,   26,  7,   28,  5,
            26,  8,   26,  7,   26,  8,   26,  9,   27,  12,  27,  11,  25,
            11,  27,  9,   25,  13,  25,  9,   25,  10,  28,  12,  26,  9,
            26,  12,  27,  11,  26,  11,  24,  11,  27,  11,  27,  13,  25,
            10,  26,  12,  23,  9,   26,  10,  24,  9,   24,  8,   26,  5,
            26,  7,   23,  4,   24,  5,   23,  4,   21,  3,   21,  1,   21,
        },
};

int main(void) {
    static csi_frame_t frame;

    csi_pre_status_t st = csi_preprocess(&example_rec, &frame);
    if (st != CSI_PRE_OK) {
        printf("csi_preprocess failed: %d\n", (int)st);
        return 1;
    }

    printf("seq=%u ts=%u rssi=%d nf=%d gain=%f dropped=%u\n",
           (unsigned)frame.meta.seq, (unsigned)frame.meta.timestamp,
           frame.meta.rssi, frame.meta.noise_floor, frame.meta.compensate_gain,
           (unsigned)frame.meta.dropped);

    printf("n=%u\n", frame.n);

    for (uint16_t k = 0; k < frame.n; k++) {
        printf("%3u: re=%4.0f im=%4.0f\n", k, frame.re[k], frame.im[k]);
    }

    return 0;
}
