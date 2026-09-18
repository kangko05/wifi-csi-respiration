/* Benchmark-only wrappers; production main and algorithms stay unchanged. */
#include "csi_resp/amplitude.h"
#include "csi_resp/phase.h"
#include <stdio.h>
#ifdef _WIN32
#include <windows.h>
static double seconds(void) {
    LARGE_INTEGER value, frequency;
    QueryPerformanceCounter(&value); QueryPerformanceFrequency(&frequency);
    return (double)value.QuadPart / frequency.QuadPart;
}
#else
#include <time.h>
static double seconds(void) {
    struct timespec t; timespec_get(&t, TIME_UTC);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
#endif
static int timed_phase(const csi_frame_t *frames, size_t n, csi_phase_result_t *result) {
    double start=seconds();
    int rc=csi_phase_process(frames,n,result);
    fprintf(stderr,"%.9f\n",seconds()-start);
    return rc;
}
static int timed_amp(const csi_frame_t *frames, size_t n,
                     const csi_amp_context_t *ctx, csi_amp_result_t *result) {
    double start=seconds();
    int rc=csi_amp_process(frames,n,ctx,result);
    fprintf(stderr,"%.9f\n",seconds()-start);
    return rc;
}
#define csi_phase_process timed_phase
#define csi_amp_process timed_amp
#include "../src/main.c"
