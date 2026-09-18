/* Port of the preserved wifi_csi_backup phase path; see docs/phase.md.
 * All spectral operations use double precision. No respiration labels enter
 * this file, and no amplitude result participates in the decision. */
#include "csi_resp/phase.h"
#include "csi_resp/filters.h"
#include <complex.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define COLS 114
#define FREQS 191
static const double PI = 3.14159265358979323846264338327950288;
static int raw_bin(int k) { return k < 57 ? k : k+3; }
static double omega(int k) { return 2*PI*(raw_bin(k)-58)*312500.; }
static double power(double complex z) { return creal(z)*creal(z)+cimag(z)*cimag(z); }
static int compare_double(const void *a, const void *b) {
    double x=*(const double *)a, y=*(const double *)b;
    return (x>y)-(x<y);
}
static double median(double *values, size_t n) {
    qsort(values,n,sizeof(*values),compare_double);
    return n%2 ? values[n/2] : (values[n/2-1]+values[n/2])/2;
}
static double wrap_angle(double a) { return a-2*PI*floor((a+PI)/(2*PI)); }

/* Bluestein chirp convolution: exact requested frequencies, without padding
 * the signal's FFT grid. The radix-2 padding is only for the convolution.
 * Kernel FFT is reused across the 114 columns. */
typedef struct {
    size_t n,m,length;
    double complex *kernel,*work,*input_chirp,*output_chirp;
} chirp_plan;
static void fft(double complex *a, size_t n, int inverse) {
    for (size_t i=1,j=0; i<n; ++i) {
        size_t bit=n>>1;
        for (; j&bit; bit>>=1) j^=bit;
        j^=bit;
        if (i<j) { double complex t=a[i]; a[i]=a[j]; a[j]=t; }
    }
    for (size_t length=2; length<=n; length*=2) {
        double complex step=cexp(I*(inverse ? 2 : -2)*PI/length);
        for (size_t start=0; start<n; start+=length) {
            double complex w=1;
            for (size_t j=0; j<length/2; ++j) {
                double complex u=a[start+j],v=a[start+j+length/2]*w;
                a[start+j]=u+v; a[start+j+length/2]=u-v; w*=step;
            }
        }
        if (length==n) break;
    }
    if (inverse) for (size_t i=0;i<n;++i) a[i]/=n;
}
static void chirp_free(chirp_plan *p) {
    free(p->kernel); free(p->work); free(p->input_chirp); free(p->output_chirp);
    memset(p,0,sizeof(*p));
}
static int chirp_init(chirp_plan *p,size_t n,size_t m,double start,double step) {
    if (!n || !m || n>SIZE_MAX-m) return 1;
    p->n=n; p->m=m; p->length=1;
    while (p->length<n+m-1) {
        if (p->length>SIZE_MAX/2) return 1;
        p->length*=2;
    }
    if (p->length>SIZE_MAX/sizeof(double complex)) return 1;
    p->kernel=calloc(p->length,sizeof(double complex));
    p->work=malloc(p->length*sizeof(double complex));
    p->input_chirp=malloc(n*sizeof(double complex));
    p->output_chirp=malloc(m*sizeof(double complex));
    if (!p->kernel || !p->work || !p->input_chirp || !p->output_chirp) {
        chirp_free(p); return 1;
    }
    for (size_t j=0;j<n;++j) {
        double square=(double)j*j;
        p->input_chirp[j]=cexp(-I*PI*(step*square+2*start*j));
        if (j) p->kernel[p->length-j]=cexp(I*PI*step*square);
    }
    for (size_t j=0;j<m;++j) {
        p->output_chirp[j]=cexp(-I*PI*step*(double)j*j);
        p->kernel[j]=conj(p->output_chirp[j]);
    }
    fft(p->kernel,p->length,0);
    return 0;
}
static void chirp_apply(chirp_plan *p,const double complex *x,size_t stride) {
    memset(p->work,0,p->length*sizeof(*p->work));
    double complex mean=0;
    for (size_t i=0;i<p->n;++i) mean+=x[i*stride];
    mean/=p->n;
    for (size_t i=0;i<p->n;++i) p->work[i]=(x[i*stride]-mean)*p->input_chirp[i];
    fft(p->work,p->length,0);
    for (size_t i=0;i<p->length;++i) p->work[i]*=p->kernel[i];
    fft(p->work,p->length,1);
    for (size_t i=0;i<p->m;++i) p->work[i]*=p->output_chirp[i];
}

/* RMS normalization then legacy Algorithm 4 (coarse + static target + WLS).
 * Neighbour unwrap operates on the retained subcarrier list, as in Python. */
static int correct_phase(double complex *h,size_t n) {
    double *tau=malloc(n*sizeof(*tau));
    double complex target[COLS]={0};
    if (!tau) return 1;
    for (size_t i=0;i<n;++i) {
        double complex *row=h+i*COLS,corr=0,sum=0;
        double rms=0;
        for (int k=0;k<COLS;++k) rms+=power(row[k]);
        rms=sqrt(rms/COLS);
        if (rms==0) rms=1;
        for (int k=0;k<COLS;++k) row[k]/=rms;
        for (int k=0;k<COLS-1;++k) corr+=row[k]*conj(row[k+1]);
        tau[i]=3.2e-6/(2*PI)*carg(corr);
        for (int k=0;k<COLS;++k) sum+=row[k]*cexp(I*omega(k)*tau[i]);
        double psi=-carg(sum);
        for (int k=0;k<COLS;++k) target[k]+=row[k]*cexp(I*(omega(k)*tau[i]+psi))/n;
    }
    double scale=0;
    for (int k=0;k<COLS;++k) scale+=power(target[k])/COLS;
    int keep[COLS],nk=0;
    for (int k=0;k<COLS;++k) if (scale<=0 || power(target[k])>.1*scale) keep[nk++]=k;
    if (nk<8) { nk=COLS; for (int k=0;k<COLS;++k) keep[k]=k; }
    for (size_t i=0;i<n;++i) {
        double complex *row=h+i*COLS,w[COLS];
        double y[COLS],weights[COLS],prev=0,offset=0,sw=0,mx=0,my=0;
        for (int j=0;j<nk;++j) {
            int k=keep[j]; w[j]=conj(row[k])*target[k]*cexp(-I*omega(k)*tau[i]);
        }
        for (int j=0;j<nk;++j) {
            double complex ref=0;
            for (int q=(j>3 ? j-3 : 0);q<nk && q<=j+3;++q) ref+=w[q];
            double angle=carg(ref);
            if (j) {
                double delta=angle-prev;
                if (fabs(delta)>PI) offset+=wrap_angle(delta)-delta;
            }
            prev=angle; angle+=offset;
            y[j]=wrap_angle(carg(w[j])-angle)+angle;
            weights[j]=cabs(w[j]);
            sw+=weights[j]; mx+=weights[j]*omega(keep[j]); my+=weights[j]*y[j];
        }
        double slope=0,intercept=0;
        if (sw>0) {
            mx/=sw; my/=sw;
            double variance=0,covariance=0;
            for (int j=0;j<nk;++j) {
                double d=omega(keep[j])-mx;
                variance+=weights[j]*d*d; covariance+=weights[j]*d*(y[j]-my);
            }
            if (variance>0) slope=covariance/variance;
            intercept=my-slope*mx;
        }
        for (int k=0;k<COLS;++k) row[k]*=cexp(I*(omega(k)*(tau[i]+slope)+intercept));
    }
    free(tau);
    return 0;
}

static int hampel(double *x,size_t n) {
    double *out=malloc(n*sizeof(*out));
    if (!out) return 1;
    for (size_t i=0;i<n;++i) {
        double values[11],dev[11];
        for (int j=-5;j<=5;++j) {
            size_t index=j<0 && i<(size_t)(-j) ? 0 : i+j;
            if (index>=n) index=n-1;
            values[j+5]=x[index];
        }
        double med=median(values,11);
        for (int j=0;j<11;++j) dev[j]=fabs(values[j]-med);
        out[i]=fabs(x[i]-med)>3*1.4826*median(dev,11) ? med : x[i];
    }
    memcpy(x,out,n*sizeof(*x)); free(out); return 0;
}
typedef struct { size_t index; double height; int keep; } peak;
static int compare_peak(const void *a,const void *b) {
    const peak *x=a,*y=b;
    if (x->height!=y->height) return x->height>y->height ? -1 : 1;
    return x->index>y->index ? -1 : x->index<y->index;
}
static int count_peaks(const double *x,size_t n,double fs,size_t *count,double *bpm) {
    peak *peaks=malloc(n*sizeof(*peaks));
    if (!peaks) return 1;
    size_t np=0;
    for (size_t i=1;i+1<n;++i) if (x[i]>x[i-1]) {
        size_t end=i;
        while (end+1<n && x[end+1]==x[i]) ++end;
        if (end+1<n && x[end]>x[end+1]) peaks[np++]=(peak){(i+end)/2,x[i],1};
        i=end;
    }
    qsort(peaks,np,sizeof(*peaks),compare_peak);
    size_t distance=(size_t)fmax(1,nearbyint(fs));
    for (size_t i=0;i<np;++i) if (peaks[i].keep)
        for (size_t j=i+1;j<np;++j) {
            size_t a=peaks[i].index,b=peaks[j].index;
            if ((a>b ? a-b : b-a)<distance) peaks[j].keep=0;
        }
    double mean=0,variance=0;
    for (size_t i=0;i<n;++i) mean+=x[i]/n;
    for (size_t i=0;i<n;++i) variance+=(x[i]-mean)*(x[i]-mean)/n;
    double threshold=.3*sqrt(variance);
    size_t first=n,last=0;
    *count=0; *bpm=0;
    for (size_t j=0;j<np;++j) if (peaks[j].keep) {
        size_t k=peaks[j].index;
        double left=x[k],right=x[k];
        for (size_t i=k;i>0;) { --i; if (x[i]>x[k]) break; left=fmin(left,x[i]); }
        for (size_t i=k+1;i<n;++i) { if (x[i]>x[k]) break; right=fmin(right,x[i]); }
        if (x[k]-fmax(left,right)<threshold) continue;
        ++*count; if (k<first) first=k; if (k>last) last=k;
    }
    if (*count>=2) *bpm=(*count-1)*fs/(last-first)*60;
    free(peaks); return 0;
}

static int analyze(double complex *h,size_t n,double fs,csi_phase_result_t *r) {
    chirp_plan plan={0};
    double spectrum[FREQS]={0},background[FREQS],energies[COLS]={0};
    double *wave=calloc(n,sizeof(*wave)),*ref=malloc(n*sizeof(*ref));
    int status=1;
    if (!wave || !ref) goto done;
    double maximum=0;
    for (int k=0;k<COLS;++k) {
        double complex mean=0;
        for (size_t i=0;i<n;++i) mean+=h[i*COLS+k]/n;
        for (size_t i=0;i<n;++i) {
            h[i*COLS+k]-=mean;
            maximum=fmax(maximum,cabs(h[i*COLS+k]));
        }
    }
    if (chirp_init(&plan,n,n,0,1.0/n)) goto done;
    for (int k=0;k<COLS;++k) {
        chirp_apply(&plan,h+k,COLS);
        for (size_t j=1;j<n;++j) {
            double freq=(j<=n/2 ? j : n-j)*fs/n;
            if (freq>=.05 && freq<=1.) energies[k]+=power(plan.work[j]);
        }
    }
    chirp_free(&plan);
    for (int j=0;j<5;++j) {
        int best=0;
        for (int k=1;k<COLS;++k) if (energies[k]>energies[best]) best=k;
        r->selected_bins[j]=raw_bin(best); energies[best]=-1;
        double complex second=0;
        for (size_t i=0;i<n;++i) second+=h[i*COLS+best]*h[i*COLS+best];
        double complex rotation=cexp(-I*.5*carg(second));
        double dot=0;
        if (!j) for (size_t i=0;i<n;++i) ref[i]=creal(h[i*COLS+best]*rotation);
        for (size_t i=0;i<n;++i) dot+=ref[i]*creal(h[i*COLS+best]*rotation);
        double sign=dot<0 ? -1 : 1;
        for (size_t i=0;i<n;++i) wave[i]+=sign*creal(h[i*COLS+best]*rotation);
    }
    if (hampel(wave,n) || butter_bandpass_zero_phase(wave,n,fs,.05,1.)) goto done;
    for (size_t i=0;i<n;++i) maximum=fmax(maximum,fabs(wave[i]));
    if (maximum<=1e-12) { r->reasons=CSI_PHASE_FLAT; status=0; goto done; }
    if (chirp_init(&plan,n,FREQS,.05/fs,.005/fs)) goto done;
    for (int k=0;k<COLS;++k) {
        chirp_apply(&plan,h+k,COLS);
        for (int j=0;j<FREQS;++j) spectrum[j]+=power(plan.work[j]);
    }
    double floor_power=0;
    for (int j=0;j<FREQS;++j) {
        double values[61];
        for (int q=-30;q<=30;++q) {
            int index=j+q; if (index<0) index=0; if (index>=FREQS) index=FREQS-1;
            values[q+30]=spectrum[index];
        }
        background[j]=median(values,61); floor_power=fmax(floor_power,background[j]);
    }
    floor_power=fmax(floor_power*1e-6,DBL_MIN);
    int best=0;
    for (int j=0;j<FREQS;++j) {
        double whitened=spectrum[j]/fmax(background[j],floor_power);
        if (whitened>r->sharpness) { r->sharpness=whitened; best=j; }
    }
    r->spectral_bpm=(.05+.005*best)*60;
    double f0=r->spectral_bpm/60;
    if (butter_bandpass_zero_phase(wave,n,fs,fmax(.7*f0,.05),fmin(1.5*f0,1.))) goto done;
    if (count_peaks(wave,n,fs,&r->n_peaks,&r->peak_count_bpm)) goto done;
    r->peak_fallback=(best==0 || best==FREQS-1) && r->peak_count_bpm>0;
    r->bpm=r->peak_fallback ? r->peak_count_bpm : r->spectral_bpm;
    r->agreement_bpm=r->peak_count_bpm>0 ? fabs(r->spectral_bpm-r->peak_count_bpm) : INFINITY;
    if (r->sharpness<=1.55) r->reasons|=CSI_PHASE_WEAK;
    if (r->agreement_bpm>=3) r->reasons|=CSI_PHASE_DISAGREEMENT;
    r->accepted=!r->reasons && r->bpm>0;
    status=0;
done:
    free(wave); free(ref); chirp_free(&plan); return status;
}

int csi_phase_process(const csi_frame_t *frames,size_t count,csi_phase_result_t *r) {
    if (!frames || !r || count<2 || count>SIZE_MAX/(COLS*sizeof(double complex))) return 1;
    memset(r,0,sizeof(*r));
    for (int j=0;j<5;++j) r->selected_bins[j]=-1;
    double *times=malloc(count*sizeof(*times)),*intervals=malloc(count*sizeof(*intervals));
    double complex *h=NULL,*uniform=NULL;
    size_t retained=0;
    int status=1;
    if (!times || !intervals) goto done;
    uint64_t elapsed=0;
    double null_sum=0,occupied_sum=0;
    int seen[COLS]={0};
    for (size_t i=0;i<count;++i) {
        if (frames[i].n!=117) goto done;
        if (i) {
            uint32_t delta=frames[i].meta.timestamp-frames[i-1].meta.timestamp;
            if (delta>=UINT32_C(0x80000000)) goto done;
            elapsed+=delta;
            if (elapsed>UINT32_MAX) goto done;
        }
        int valid=1;
        for (int k=0;k<117;++k) {
            double re=frames[i].re[k],im=frames[i].im[k];
            if (!isfinite(re) || !isfinite(im)) goto done;
            if (k>=57 && k<=59) null_sum+=hypot(re,im);
            else {
                occupied_sum+=hypot(re,im);
                if (!frames[i].valid[k]) valid=0;
                if (frames[i].valid[k] && (re!=0 || im!=0)) seen[k<57 ? k : k-3]=1;
            }
        }
        if (valid) { times[retained]=elapsed/1e6; intervals[retained++]=(double)i; }
    }
    if (retained<2 || occupied_sum<=0 || (null_sum/3)/(occupied_sum/COLS)>.05) goto done;
    for (int k=0;k<COLS;++k) if (!seen[k]) goto done;
    r->max_gap_s=fmax(times[0],elapsed/1e6-times[retained-1]);
    h=malloc(retained*COLS*sizeof(*h));
    if (!h) goto done;
    for (size_t j=0;j<retained;++j) {
        size_t i=(size_t)intervals[j];
        for (int k=0;k<COLS;++k) h[j*COLS+k]=frames[i].re[raw_bin(k)]+I*frames[i].im[raw_bin(k)];
        if (j) r->max_gap_s=fmax(r->max_gap_s,times[j]-times[j-1]);
    }
    if (r->max_gap_s>.1+1e-9 || times[retained-1]-times[0]<20) goto done;
    for (size_t i=1;i<retained;++i) intervals[i-1]=times[i]-times[i-1];
    double dt=median(intervals,retained-1);
    if (!(dt>0) || 1/dt<=2/.99) goto done;
    double fs=1/dt, grid_count=floor((times[retained-1]-times[0])*fs)+1;
    if (grid_count>SIZE_MAX/(COLS*sizeof(*uniform))) goto done;
    size_t n=(size_t)grid_count;
    if (correct_phase(h,retained)) goto done;
    uniform=malloc(n*COLS*sizeof(*uniform));
    if (!uniform) goto done;
    size_t left=0;
    for (size_t i=0;i<n;++i) {
        double t=times[0]+i/fs;
        while (left+1<retained && times[left+1]<=t) ++left;
        size_t right=left+1<retained ? left+1 : left;
        double fraction=right==left ? 0 : (t-times[left])/(times[right]-times[left]);
        for (int k=0;k<COLS;++k) uniform[i*COLS+k]=h[left*COLS+k]+fraction*(h[right*COLS+k]-h[left*COLS+k]);
    }
    free(h); h=NULL;
    r->n_samples=n; r->n_retained=retained; r->sample_rate_hz=fs;
    r->start_s=times[0]; r->duration_s=(n-1)/fs;
    status=analyze(uniform,n,fs,r);
done:
    free(h); free(uniform); free(times); free(intervals); return status;
}
