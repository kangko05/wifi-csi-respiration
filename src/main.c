#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "csi_resp/amplitude.h"
#include "csi_resp/csi_record.h"
#include "csi_resp/csi_utils.h"
#include "csi_resp/filters.h"
#include "csi_resp/phase.h"
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

static int synthetic_demo(void) {
    const firwin_config_t config = {
        .target_fs = 10.0f,
        .half_width_s = 1.0f,
        .beta = 8.0f,
    };

    csi_amp_context_t context = {0};
    static csi_amp_result_t result;
    csi_frame_t *frames = NULL;
    int status = 1;
    if (csi_amp_init(&context, &config) != 0) {
        printf("amplitude initialization failed\n");
        return 1;
    }

    /* 실행 예제: 실제 패킷 하나의 전처리 결과를 바탕으로 합성 시계열 생성.
     * 원본 수집 데이터의 호흡 분석 결과가 아니다.
     * 첫 세 칸만 사용: 0.25Hz 변동 / 일정 진폭 / 무효 서브캐리어. */
    csi_frame_t frame;

    if (csi_preprocess(&example_rec, &frame) != CSI_PRE_OK) {
        goto cleanup;
    }

    const double duration_s = 60.0;
    size_t n_frames = (size_t)(duration_s * 1000000.0 / CSI_INTERP_STEP_US) + 1;
    frames = malloc(n_frames * sizeof(*frames));

    if (frames == NULL) {
        goto cleanup;
    }

    for (size_t i = 0; i < n_frames; i++) {
        frames[i] = frame;
        frames[i].n = 3;
        frames[i].meta.timestamp += (uint32_t)(i * CSI_INTERP_STEP_US);
        double t = (double)i * CSI_INTERP_STEP_US / 1000000.0;
        frames[i].re[0] = (float)(10.0 + sin(2.0 * CSI_PI * 0.25 * t));
        frames[i].im[0] = 0;
        frames[i].re[1] = 10;
        frames[i].im[1] = 0;
        frames[i].valid[2] = 0;
    }

    if (csi_amp_process(frames, n_frames, &context, &result) != 0) {
        printf("amplitude processing failed\n");
        goto cleanup;
    }

    printf("SYNTHETIC example: 0.25 Hz (15 bpm), not a real measurement\n");
    printf("samples=%zu fs=%.3f duration=%.2f selected_bin=%d best_bin=%d\n",
           result.n_samples, result.sample_rate_hz, result.duration_s,
           result.selected_bin, result.best_bin);

    for (size_t k = 0; k < result.n_bins; k++) {
        const csi_amp_bin_result_t *bin = &result.bins[k];
        printf("bin=%zu accepted=%d reasons=0x%x", k, bin->accepted,
               (unsigned)bin->reasons);
        if (bin->has_psd) {
            printf(" PSD=%.3f bpm", bin->psd_bpm);
        }
        if (bin->has_acf) {
            printf(" ACF=%.3f bpm (peak=%.3f)", bin->acf_bpm, bin->acf_peak);
        }
        printf("\n");
    }

    status = 0;

cleanup:
    free(frames);
    csi_amp_free(&context);
    return status;
}

/* --stdin 프로토콜 (공백 구분 텍스트):
 * 첫 줄: 레코드 수
 * 각 레코드: seq timestamp dropped gain rssi noise fft_gain agc_gain channel
 *            bb_format first_word_invalid sig_len len, 이어서 len개의 signed
 * byte. Python은 base64만 풀고 실제 전처리/분석은 여기서 수행한다. */
static int read_record(csi_record_t *rec, int phase_method) {
    int rssi, noise, fft_gain;
    unsigned agc, channel, format, invalid, sig_len, len;

    if (scanf("%" SCNu32 " %" SCNu32 " %" SCNu32
              " %f %d %d %d %u %u %u %u %u %u",
              &rec->seq, &rec->timestamp, &rec->dropped, &rec->compensate_gain,
              &rssi, &noise, &fft_gain, &agc, &channel, &format, &invalid,
              &sig_len, &len) != 13) {
        return 1;
    }

    if (rssi < -128 || rssi > 127 || noise < -128 || noise > 127 ||
        fft_gain < -128 || fft_gain > 127 || agc > 255 || channel > 255 ||
        format > 255 || invalid > 255 || sig_len > 65535 || len > CSI_BUF_MAX) {
        return 1;
    }

    rec->rssi = (int8_t)rssi;
    rec->noise_floor = (int8_t)noise;
    rec->fft_gain = (int8_t)fft_gain;
    rec->agc_gain = (uint8_t)agc;
    rec->channel = (uint8_t)channel;
    rec->bb_format = (uint8_t)format;
    rec->first_word_invalid = (uint8_t)invalid;
    rec->sig_len = (uint16_t)sig_len;
    rec->len = (uint16_t)len;

    if (phase_method && (len != 234 || format != 2)) {
        return 1;
    }

    for (unsigned j = 0; j < len; j++) {
        int value;
        if (scanf("%d", &value) != 1 || value < -128 || value > 127) {
            return 1;
        }
        rec->buf[j] = (int8_t)value;
    }

    return 0;
}

static void print_phase_result(const csi_phase_result_t *phase, size_t used,
                               size_t rejected) {
    printf("{\"method\":\"phase\",\"status\":\"%s\",\"n_frames\":%zu,"
           "\"preprocess_rejected\":%zu,\"n_samples\":%zu,\"n_retained\":%zu,"
           "\"fs\":%.17g,\"start_s\":%.17g,\"duration_s\":%.17g,\"max_gap_s\":"
           "%.17g,"
           "\"accepted\":%d,\"reasons\":%u,\"diagnostic_bpm\":%.17g,"
           "\"spectral_bpm\":%.17g,\"peak_count_bpm\":%.17g,\"sharpness\":%."
           "17g,"
           "\"n_peaks\":%zu,\"peak_fallback\":%d,\"agreement_bpm\":",
           phase->accepted ? "periodic_candidate" : "weak_evidence", used,
           rejected, phase->n_samples, phase->n_retained, phase->sample_rate_hz,
           phase->start_s, phase->duration_s, phase->max_gap_s, phase->accepted,
           phase->reasons, phase->bpm, phase->spectral_bpm,
           phase->peak_count_bpm, phase->sharpness, phase->n_peaks,
           phase->peak_fallback);

    if (isfinite(phase->agreement_bpm)) {
        printf("%.17g", phase->agreement_bpm);
    } else {
        printf("null");
    }

    printf(",\"selected_bins\":[");
    for (size_t i = 0; i < phase->n_selected; ++i) {
        printf("%s%d", i ? "," : "", phase->selected_bins[i]);
    }
    printf("]}\n");
}

static void print_amplitude_result(const csi_amp_result_t *result, size_t used,
                                   size_t rejected) {
    printf("{\"status\":\"%s\",\"n_frames\":%zu,\"preprocess_rejected\":%zu,"
           "\"n_samples\":%zu,\"n_fft\":%zu,\"start_us\":%" PRIu32
           ",\"step_us\":%" PRIu64 ","
           "\"fs\":%.17g,\"duration_s\":%.17g,\"resolution_hz\":%.17g,"
           "\"selected_bin\":%d,\"best_bin\":%d,\"bins\":[",
           result->selected_bin >= 0 ? "periodic_candidate" : "weak_evidence",
           used, rejected, result->n_samples, result->n_fft, result->start_us,
           result->step_us, result->sample_rate_hz, result->duration_s,
           result->resolution_hz, result->selected_bin, result->best_bin);

    for (size_t k = 0; k < result->n_bins; k++) {
        const csi_amp_bin_result_t *b = &result->bins[k];
        printf("%s{\"bin\":%zu,\"eligible\":%d,\"accepted\":%d,\"reasons\":%u,"
               "\"psd_bpm\":",
               k ? "," : "", k, b->eligible, b->accepted, (unsigned)b->reasons);
        if (b->has_psd) {
            printf("%.17g", b->psd_bpm);
        } else {
            printf("null");
        }
        printf(",\"acf_bpm\":");
        if (b->has_acf) {
            printf("%.17g", b->acf_bpm);
        } else {
            printf("null");
        }
        printf(",\"acf_peak\":%.17g,\"concentration\":%.17g,\"score\":%.17g}",
               b->acf_peak, b->concentration, b->score);
    }

    printf("]}\n");
}

static int run_phase(const csi_frame_t *frames, size_t used, size_t rejected) {
    csi_phase_result_t result;

    if (csi_phase_process(frames, used, &result)) {
        printf("{\"method\":\"phase\",\"status\":\"process_error\",\"n_"
               "frames\":%zu}\n",
               used);
        return 1;
    }

    print_phase_result(&result, used, rejected);
    return 0;
}

static int run_amplitude(const csi_frame_t *frames, size_t used,
                         size_t rejected) {
    const firwin_config_t config = {
        .target_fs = 10, .half_width_s = 1, .beta = 8};
    csi_amp_context_t ctx = {0};
    static csi_amp_result_t result;
    int status = 1;

    if (csi_amp_init(&ctx, &config) ||
        csi_amp_process(frames, used, &ctx, &result)) {
        printf("{\"status\":\"process_error\",\"n_frames\":%zu,\"preprocess_"
               "rejected\":%zu}\n",
               used, rejected);
        goto cleanup;
    }

    print_amplitude_result(&result, used, rejected);
    status = 0;

cleanup:
    csi_amp_free(&ctx);
    return status;
}

static int run_stdin(int phase_method) {
    size_t count;

    if (scanf("%zu", &count) != 1 || count < 2 || count > 1000000 ||
        count > SIZE_MAX / sizeof(csi_frame_t)) {
        return 1;
    }

    csi_frame_t *frames = malloc(count * sizeof(*frames));
    size_t used = 0, rejected = 0;
    int status = 1;

    if (frames == NULL) {
        return 1;
    }

    for (size_t i = 0; i < count; i++) {
        csi_record_t rec = {0};

        if (read_record(&rec, phase_method)) {
            goto cleanup;
        }

        if (csi_preprocess(&rec, &frames[used]) == CSI_PRE_OK) {
            used++;
        } else {
            rejected++;
        }
    }

    status = phase_method ? run_phase(frames, used, rejected)
                          : run_amplitude(frames, used, rejected);

cleanup:
    free(frames);
    return status;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--stdin") == 0) {
        return run_stdin(0);
    }
    if (argc == 3 && strcmp(argv[1], "--stdin") == 0 &&
        strcmp(argv[2], "--phase") == 0) {
        return run_stdin(1);
    }

    if (argc != 1) {
        fprintf(stderr, "Usage: csi_resp_main [--stdin [--phase]]\n");
        return 1;
    }

    return synthetic_demo();
}
