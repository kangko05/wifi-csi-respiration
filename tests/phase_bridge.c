/* Test-only access to internal numerical stages. */
#include "../src/phase.c"
int test_phase_correct(double complex *h,size_t n) {
    csi_phase_config_t config=csi_phase_default_config();
    phase_parameters p;
    if (phase_parameters_init(&p,&config)) return 1;
    return correct_phase(h,n,&p);
}
int test_chirp(const double complex *x,size_t n,size_t m,double start,double step,double complex *out) {
    chirp_plan p={0};
    if (chirp_init(&p,n,m,start,step)) return 1;
    chirp_apply(&p,x,1); memcpy(out,p.work,m*sizeof(*out)); chirp_free(&p); return 0;
}
int test_peaks(const double *x,size_t n,double fs,size_t *count,double *bpm) {
    csi_phase_config_t config=csi_phase_default_config();
    phase_parameters p;
    if (phase_parameters_init(&p,&config)) return 1;
    return count_peaks(x,n,fs,count,bpm,&p);
}
void test_phase_default_config(csi_phase_config_t *out) { *out=csi_phase_default_config(); }
int test_phase_pipeline_config(const float *values,const uint8_t *valid,const uint32_t *times,
                        size_t n,size_t bins,const csi_phase_config_t *config,csi_phase_result_t *out) {
    if (bins>CSI_MAX_SUBCARRIERS) return 1;
    csi_frame_t *frames=calloc(n,sizeof(*frames));
    if (!frames) return 1;
    for (size_t i=0;i<n;++i) {
        frames[i].n=(uint16_t)bins; frames[i].meta.timestamp=times[i];
        for (size_t k=0;k<bins;++k) {
            frames[i].re[k]=values[2*(i*bins+k)]; frames[i].im[k]=values[2*(i*bins+k)+1];
            frames[i].valid[k]=valid[i*bins+k];
        }
    }
    int status=csi_phase_process_with_config(frames,n,config,out); free(frames); return status;
}
int test_phase_pipeline(const float *values,const uint8_t *valid,const uint32_t *times,
                        size_t n,size_t bins,csi_phase_result_t *out) {
    csi_phase_config_t config=csi_phase_default_config();
    return test_phase_pipeline_config(values,valid,times,n,bins,&config,out);
}
