#include "csi_resp/amplitude.h"
#include "csi_resp/preprocess.h"
#include <stdlib.h>
#include <assert.h>


/* Linker-wrapped allocator: measure only the raw call and inject failures. */
void *__real_malloc(size_t n);
void __real_free(void *p);
static struct { void *p; size_t n; } allocations[16];
static int tracking, allocation_index, fail_at = -1;
static size_t live, peak;
void test_raw_fail_at(int index) { fail_at = index; }
size_t test_raw_peak(void) { return peak; }
size_t test_raw_live(void) { return live; }
void *__wrap_malloc(size_t n) {
    if (tracking && allocation_index++ == fail_at) return NULL;
    void *p = __real_malloc(n);
    if (p && tracking) {
        for (size_t i=0;i<16;++i) {
            if (!allocations[i].p) {
                allocations[i].p=p; allocations[i].n=n;
                live+=n; if (live>peak) peak=live;
                return p;
            }
        }
        assert(0);
    }
    return p;
}
void __wrap_free(void *p) {
    if (p) for (size_t i=0;i<16;++i) {
        if (allocations[i].p==p) {
            live-=allocations[i].n; allocations[i].p=NULL; break;
        }
    }
    __real_free(p);
}

typedef struct { const csi_record_t *records; size_t n, head; } raw_ring;
static const csi_record_t *read_raw(const void *user, size_t i) {
    const raw_ring *ring = user;
    return &ring->records[(ring->head + i) % ring->n];
}

static void check_trace(void *user, csi_amp_stage_t stage, size_t bin, size_t bins) {
    unsigned *calls = user;
    assert(stage < CSI_AMP_STAGE_COUNT);
    assert(bins == 0 || bin < bins);
    ++*calls;
}

int test_raw_parity(const csi_record_t *records, size_t n, size_t head,
                     float target_fs, float half_width,
                     csi_amp_result_t *old_result, csi_amp_result_t *new_result,
                     int *old_status, int *new_status) {
    if (!n || head >= n) return 1;
    /* Larger table also exercises reuse by smaller power-of-two transforms. */
    static double twiddles[65536];
    static csi_fft_plan_t plan;
    if (!plan.twiddles && csi_fft_plan_init(&plan, 65536, twiddles, 65536)) return 1;
    raw_ring ring = {records, n, head};
    unsigned trace_calls = 0;
    csi_amp_raw_source_t source = {.read=read_raw, .user=&ring,
        .trace=check_trace, .trace_user=&trace_calls, .fft_plan=&plan};
    csi_frame_t *frames = calloc(n, sizeof(*frames));
    csi_amp_context_t ctx = {0};
    firwin_config_t cfg = {.target_fs=target_fs, .half_width_s=half_width, .beta=8};
    if (!frames || csi_amp_init(&ctx, &cfg)) { free(frames); return 1; }
    for (size_t i=0; i<n; ++i) {
        if (csi_preprocess(read_raw(&ring, i), &frames[i]) != CSI_PRE_OK) {
            csi_amp_free(&ctx); free(frames); return 1;
        }
    }
    *old_status = csi_amp_process(frames, n, &ctx, old_result);
    assert(live == 0); peak=0; allocation_index=0; tracking=1;
    *new_status = csi_amp_process_raw(&source, n, &ctx, new_result);
    assert(trace_calls > 0);
    tracking=0;
    csi_amp_free(&ctx);
    free(frames);
    return 0;
}

int test_fixed_allocation_failure(const csi_record_t *records, size_t n, int index) {
    raw_ring ring={records,n,0};
    csi_amp_context_t ctx={0};
    firwin_config_t cfg={.target_fs=10,.half_width_s=1,.beta=8};
    csi_amp_result_t *result=malloc(sizeof(*result));
    if (!result || csi_amp_init(&ctx,&cfg)) { free(result);return -1; }
    csi_amp_raw_source_t source={.read=read_raw,.user=&ring,.fixed_preprocess=1};
    assert(live==0);peak=0;allocation_index=0;fail_at=index;tracking=1;
    int status=csi_amp_process_raw(&source,n,&ctx,result);
    tracking=0;fail_at=-1;
    assert(live==0);
    csi_amp_free(&ctx);free(result);
    return status;
}
