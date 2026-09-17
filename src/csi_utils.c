#include <math.h>

#include "csi_resp/csi_record.h"
#include "csi_resp/csi_utils.h"

float csi_abs(float re, float im) { return sqrtf(re * re + im * im); }

void csi_amplitude(const csi_frame_t *frame, float *amp) {
    for (uint16_t k = 0; k < frame->n; k++) {
        if (frame->valid[k]) {
            float re = frame->re[k];
            float im = frame->im[k];

            amp[k] = csi_abs(re, im);
        } else {
            amp[k] = 0.0f;
        }
    }
}
