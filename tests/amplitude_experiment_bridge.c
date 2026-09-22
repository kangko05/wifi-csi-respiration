#include "csi_resp/amplitude.h"
#include <stdlib.h>

static const csi_record_t *read_record(const void *user, size_t i) {
    return &((const csi_record_t *)user)[i];
}
int test_experiment(const csi_record_t *records, size_t n, int float_math,
                    int padding, float rate, size_t bins, int streaming,
                    csi_amp_result_t *result) {
    static double table[65536];
    static csi_fft_plan_t plan;
    if (!plan.twiddles && csi_fft_plan_init(&plan, 65536, table, 65536)) return 1;
    static int16_t q15_table[65536];
    static csi_fft_q15_plan_t q15_plan;
    if (!q15_plan.twiddles && csi_fft_q15_plan_init(&q15_plan, 65536, q15_table, 65536)) return 1;
    int q15 = float_math == 2 || float_math == 4;
    int fixed_preprocess = float_math == 3 || float_math == 4;
    if (q15 || fixed_preprocess) float_math = 1;
    csi_amp_context_t ctx = {0};
    firwin_config_t cfg = {.target_fs=rate, .half_width_s=1, .beta=8};
    if (!n || csi_amp_init(&ctx, &cfg)) return 1;
    ctx.analysis.fft_oversampling = padding;
    csi_amp_raw_source_t source = {.read=read_record, .user=records,
        .fft_plan=&plan, .float_math=float_math, .max_bins=bins,
        .q15_plan=q15 ? &q15_plan : NULL, .fixed_preprocess=fixed_preprocess};
    csi_amp_stream_t *stream = NULL;
    int status = 1;
    if (streaming) {
        stream = csi_amp_stream_create(&ctx, records[0].len/2, n, float_math);
        if (!stream) goto done;
        for (size_t i=0;i<n;++i)
            if (csi_amp_stream_push(stream, &records[i])) goto done;
        if (csi_amp_stream_finish(stream)) goto done;
        source.prepared = csi_amp_stream_data(stream);
        source.prepared_rows = csi_amp_stream_rows(stream);
    }
    status = csi_amp_process_raw(&source, n, &ctx, result);
done:
    csi_amp_stream_free(stream);
    csi_amp_free(&ctx);
    return status;
}

#include <math.h>
#include <string.h>

typedef struct {
    float *values;
    size_t capacity, counts[CSI_MAX_SUBCARRIERS], compared;
    int comparing, mismatch;
    double maximum, square_sum;
} preprocess_observer;

static void observe_preprocess(void *user, size_t bin, const float *data, size_t n) {
    preprocess_observer *o = user;
    if (bin >= CSI_MAX_SUBCARRIERS || n > o->capacity) { o->mismatch=1; return; }
    if (!o->comparing) {
        o->counts[bin]=n;
        memcpy(o->values+bin*o->capacity,data,n*sizeof(float));
    } else {
        if (o->counts[bin]!=n) { o->mismatch=1; return; }
        for (size_t i=0;i<n;++i) {
            double delta=(double)data[i]-o->values[bin*o->capacity+i];
            if (fabs(delta)>o->maximum) o->maximum=fabs(delta);
            o->square_sum+=delta*delta; ++o->compared;
        }
    }
}

int test_fixed_preprocess_error(const csi_record_t *records, size_t n,
                                 float rate, float half, double *errors, int *statuses) {
    if (n<2 || !records[0].len) return 1;
    size_t capacity=(uint32_t)(records[n-1].timestamp-records[0].timestamp)/CSI_INTERP_STEP_US+1;
    size_t bins=records[0].len/2;
    preprocess_observer observer={.capacity=capacity};
    observer.values=malloc(capacity*bins*sizeof(float));
    csi_amp_result_t *result=malloc(sizeof(*result));
    csi_amp_context_t ctx={0};
    firwin_config_t cfg={.target_fs=rate,.half_width_s=half,.beta=8};
    if (!observer.values || !result || csi_amp_init(&ctx,&cfg)) {
        free(observer.values);free(result);return 1;
    }
    csi_amp_raw_source_t source={.read=read_record,.user=records,
        .observe_reduced=observe_preprocess,.observe_user=&observer};
    statuses[0]=csi_amp_process_raw(&source,n,&ctx,result);
    source.fixed_preprocess=1; observer.comparing=1;
    statuses[1]=csi_amp_process_raw(&source,n,&ctx,result);
    errors[0]=observer.maximum;
    errors[1]=observer.compared ? sqrt(observer.square_sum/observer.compared) : 0;
    errors[2]=(double)observer.compared;
    csi_amp_free(&ctx);free(result);free(observer.values);
    return observer.mismatch;
}
