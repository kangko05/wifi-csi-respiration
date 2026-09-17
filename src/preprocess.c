#include <math.h>

#include "csi_resp/csi_record.h"
#include "csi_resp/preprocess.h"

csi_pre_status_t csi_preprocess(const csi_record_t *rec, csi_frame_t *out) {
    if (rec->len == 0 || rec->len > CSI_BUF_MAX)
        return CSI_PRE_ERR_LEN_RANGE;
    if (rec->len % 2 != 0)
        return CSI_PRE_ERR_LEN_ODD;
    if (!isfinite(rec->compensate_gain))
        return CSI_PRE_ERR_GAIN_NONFINITE;

    out->meta.seq = rec->seq;
    out->meta.timestamp = rec->timestamp;
    out->meta.dropped = rec->dropped;
    out->meta.compensate_gain = rec->compensate_gain;
    out->meta.rssi = rec->rssi;
    out->meta.noise_floor = rec->noise_floor;

    out->n = rec->len / 2;

    for (uint16_t k = 0; k < out->n; k++) {
        out->im[k] = rec->buf[2 * k];
        out->re[k] = rec->buf[2 * k + 1];

        out->valid[k] = 1;
    }

    if (rec->first_word_invalid && out->n >= 2) {
        out->valid[0] = 0;
        out->valid[1] = 0;
    }

    return CSI_PRE_OK;
}
