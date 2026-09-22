"""C phase numerical and input-contract checks against preserved Python/SciPy."""
import ctypes as c
from pathlib import Path
import subprocess
import sys
import unittest
import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/vendor/wifi-csi-proto/src"))
from csi_pipeline._vendor.wifi_csi_backup.preprocess import phase
from csi_pipeline.phase_cir import decide_features
DP = c.POINTER(c.c_double)
FP = c.POINTER(c.c_float)
UP = c.POINTER(c.c_uint8)
TP = c.POINTER(c.c_uint32)

class PhaseResult(c.Structure):
    _fields_ = [(k, c.c_size_t) for k in ("n_samples", "n_retained", "n_peaks")] + [
        (k, c.c_double) for k in ("start_s", "sample_rate_hz", "duration_s", "max_gap_s",
        "bpm", "spectral_bpm", "peak_count_bpm", "sharpness", "agreement_bpm")] + [
        ("accepted", c.c_int), ("peak_fallback", c.c_int), ("reasons", c.c_uint),
        ("selected_bins", c.c_int * 256)] + [(k,c.c_size_t) for k in
            ("n_selected","n_columns","n_frequencies")]

class PhaseBin(c.Structure):
    _fields_ = [("frequency_hz",c.c_double),("role",c.c_int)]

class PhaseConfig(c.Structure):
    _fields_ = [("n_bins",c.c_size_t),("bins",PhaseBin*256)] + [
        (k,c.c_double) for k in ("subcarrier_spacing_hz","min_hz","max_hz","frequency_step_hz")] + [
        ("top_columns",c.c_size_t),("whiten_bins",c.c_size_t)] + [
        (k,c.c_double) for k in ("max_gap_s","min_duration_s","max_null_ratio","min_sharpness","max_agreement_bpm")]

class PhaseReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "build/phase_reference.dll"
        path.parent.mkdir(exist_ok=True)
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Wpedantic", "-shared",
            "-I", str(ROOT / "include"), str(ROOT / "tests/phase_bridge.c"),
            str(ROOT / "src/filters.c"), str(ROOT / "src/csi_utils.c"),
            "-o", str(path), "-lm"], check=True)
        cls.lib = c.CDLL(str(path))
        cls.lib.test_chirp.argtypes = [DP,c.c_size_t,c.c_size_t,c.c_double,c.c_double,DP]
        cls.lib.test_phase_correct.argtypes = [DP,c.c_size_t]
        cls.lib.butter_bandpass_zero_phase.argtypes = [DP,c.c_size_t,c.c_double,c.c_double,c.c_double]
        cls.lib.test_peaks.argtypes = [DP,c.c_size_t,c.c_double,c.POINTER(c.c_size_t),DP]
        cls.lib.test_phase_pipeline.argtypes = [FP,UP,TP,c.c_size_t,c.c_size_t,c.POINTER(PhaseResult)]
        cls.lib.test_phase_default_config.argtypes = [c.POINTER(PhaseConfig)]
        cls.lib.test_phase_pipeline_config.argtypes = [FP,UP,TP,c.c_size_t,c.c_size_t,
                                                      c.POINTER(PhaseConfig),c.POINTER(PhaseResult)]
        cls.lib.csi_fft_complex.argtypes = cls.lib.csi_ifft_complex.argtypes = [DP,c.c_size_t]

    def test_shared_complex_fft(self):
        rng=np.random.default_rng(52)
        for n in (1, 8, 1024):
            values=np.ascontiguousarray(rng.normal(size=n)+1j*rng.normal(size=n))
            original=values.copy()
            pointer=values.ctypes.data_as(DP)
            self.assertEqual(self.lib.csi_fft_complex(pointer,n),0)
            np.testing.assert_allclose(values,np.fft.fft(original),atol=1e-11,rtol=1e-11)
            self.assertEqual(self.lib.csi_ifft_complex(pointer,n),0)
            np.testing.assert_allclose(values,original,atol=1e-12,rtol=1e-12)

        for function in (self.lib.csi_fft_complex,self.lib.csi_ifft_complex):
            self.assertEqual(function(None,8),1)
            self.assertEqual(function(pointer,0),1)
            self.assertEqual(function(pointer,3),1)

    def test_arbitrary_frequency_spectrum(self):
        rng = np.random.default_rng(102)
        for n,m,start,step in ((97,97,0,1/97),(1201,191,.05/100,.005/100)):
            x = np.ascontiguousarray(rng.normal(size=n)+1j*rng.normal(size=n))
            actual = np.empty(m, dtype=np.complex128)
            self.assertEqual(self.lib.test_chirp(x.ctypes.data_as(DP),n,m,start,step,actual.ctypes.data_as(DP)),0)
            expected = np.exp(-2j*np.pi*np.outer(start+step*np.arange(m),np.arange(n))) @ (x-x.mean())
            np.testing.assert_allclose(actual,expected,rtol=1e-9,atol=1e-9)

    def test_los_correction(self):
        rng=np.random.default_rng(96)
        freq=np.r_[np.arange(-58,-1),np.arange(2,59)]*312500.
        h=np.ascontiguousarray((1+.1*rng.normal(size=(180,114))) *
            np.exp(1j*(rng.normal(size=(180,1)) + freq*rng.normal(0,1e-7,size=(180,1)))))
        normalized=h/np.sqrt(np.mean(np.abs(h)**2,axis=1))[:,None]
        expected=phase._apply(normalized,freq,*phase.los_wls(normalized,freq))
        self.assertEqual(self.lib.test_phase_correct(h.ctypes.data_as(DP),len(h)),0)
        np.testing.assert_allclose(h,expected,rtol=1e-10,atol=1e-10)

    def test_butterworth_and_peak_count(self):
        rng=np.random.default_rng(21)
        for fs,low,high in ((100.,.05,1.),(99.93,.175,.375),(20.,.05,.075)):
            x=rng.normal(size=5000)+np.sin(2*np.pi*.25*np.arange(5000)/fs)
            actual=x.copy()
            expected=signal.sosfiltfilt(signal.butter(4,[low,high],fs=fs,btype="bandpass",output="sos"),x)
            self.assertEqual(self.lib.butter_bandpass_zero_phase(actual.ctypes.data_as(DP),len(x),fs,low,high),0)
            np.testing.assert_allclose(actual,expected,rtol=1e-7,atol=1e-8)
            count=c.c_size_t(); bpm=c.c_double()
            self.lib.test_peaks(actual.ctypes.data_as(DP),len(x),fs,c.byref(count),c.byref(bpm))
            peaks,_=signal.find_peaks(actual,distance=max(1,round(fs)),prominence=.3*np.std(actual))
            self.assertEqual(count.value,len(peaks))
            target=(len(peaks)-1)*fs/(peaks[-1]-peaks[0])*60 if len(peaks)>1 else 0
            self.assertAlmostEqual(bpm.value,target,places=10)

    def pipeline(self,h,valid=None,times=None,config=None):
        h=np.ascontiguousarray(h,dtype=np.complex64)
        valid=np.ones(h.shape,dtype=np.uint8) if valid is None else np.ascontiguousarray(valid,dtype=np.uint8)
        times=np.asarray((np.arange(len(h))*10000+4294000000)%2**32,dtype=np.uint32) if times is None else times
        out=PhaseResult()
        args=(h.ctypes.data_as(FP),valid.ctypes.data_as(UP),times.ctypes.data_as(TP),len(h),h.shape[1])
        status=(self.lib.test_phase_pipeline(*args,c.byref(out)) if config is None else
                self.lib.test_phase_pipeline_config(*args,c.byref(config),c.byref(out)))
        return status,out

    def test_flat_wrap_masks_and_gaps(self):
        h=np.ones((2101,117),dtype=np.complex64); h[:,57:60]=0
        status,out=self.pipeline(h)
        self.assertEqual(status,0); self.assertEqual(out.reasons,1); self.assertFalse(out.accepted)
        valid=np.ones(h.shape,dtype=np.uint8); valid[500:502,0]=0
        status,out=self.pipeline(h,valid)
        self.assertEqual(status,0); self.assertEqual(out.n_retained,len(h)-2)
        valid[500:520,0]=0
        self.assertNotEqual(self.pipeline(h,valid)[0],0)
        self.assertNotEqual(self.pipeline(h[:1000])[0],0)
        self.assertNotEqual(self.pipeline(h[:,:116])[0],0)
        h[:,57]=1
        self.assertNotEqual(self.pipeline(h)[0],0)

    def test_complete_synthetic_pipeline(self):
        rng=np.random.default_rng(410)
        t=np.arange(2101)/100
        columns=np.r_[np.arange(57),np.arange(60,117)]
        frequencies=(columns-58)*312500.
        static=np.exp(1j*np.linspace(-1,1,114))
        moving=.08*np.exp(1j*np.linspace(0,8,114))
        clean=static+np.sin(2*np.pi*.25*t[:,None])*moving
        raw=clean*np.exp(1j*(rng.normal(size=(len(t),1)) +
             frequencies*rng.normal(0,1e-8,size=(len(t),1))))
        h=np.zeros((len(t),117),dtype=np.complex64); h[:,columns]=raw
        status,out=self.pipeline(h)
        self.assertEqual(status,0)
        normalized=h[:,columns].astype(np.complex128)
        normalized/=np.sqrt(np.mean(abs(normalized)**2,axis=1))[:,None]
        corrected=phase._apply(normalized,frequencies,*phase.los_wls(normalized,frequencies))
        fs=1/np.median(np.diff(t)); grid=np.arange(int(np.floor(t[-1]*fs))+1)/fs
        uniform=np.column_stack([np.interp(grid,t,col.real)+1j*np.interp(grid,t,col.imag)
                                 for col in corrected.T])
        dynamic=uniform-uniform.mean(axis=0)
        expected=decide_features("phase",dynamic,dynamic,fs,5,columns,"original_buffer_column")
        self.assertEqual(out.accepted,expected.accepted)
        self.assertEqual(out.n_peaks,expected.n_peaks)
        self.assertAlmostEqual(out.bpm,expected.diagnostic_bpm,places=8)
        self.assertAlmostEqual(out.sharpness,expected.sharpness,places=7)
        self.assertAlmostEqual(out.peak_count_bpm,expected.peak_count_bpm,places=8)
        self.assertEqual(list(out.selected_bins)[:out.n_selected],list(expected.selected_feature_indices))

    def test_configured_layout_and_frequency_grid(self):
        cfg=PhaseConfig(); self.lib.test_phase_default_config(c.byref(cfg))
        cfg.n_bins=13; cfg.top_columns=3; cfg.whiten_bins=7
        cfg.min_hz=.1; cfg.max_hz=.6; cfg.frequency_step_hz=.02
        used=[k for k in range(13) if k not in (4,5,12)]
        for k in range(13):
            cfg.bins[k].frequency_hz=(k-6)*cfg.subcarrier_spacing_hz
            cfg.bins[k].role=1 if k in used else (2 if k in (4,5) else 0)

        t=np.arange(3001)/100
        h=np.zeros((len(t),13),dtype=np.complex64)
        h[:,used]=np.exp(1j*np.linspace(-1,1,len(used))) + .08*np.sin(2*np.pi*.3*t[:,None])*np.exp(1j*np.linspace(0,8,len(used)))
        h[:,12]=complex(float('nan'),0)  # Explicitly skipped bin, not an inferred null.
        status,out=self.pipeline(h,config=cfg)
        self.assertEqual(status,0)
        self.assertEqual((out.n_columns,out.n_frequencies,out.n_selected),(10,26,3))
        self.assertAlmostEqual(out.spectral_bpm,18.,places=8)
        self.assertTrue(set(out.selected_bins[:out.n_selected]).issubset(used))

        cfg.max_hz=.59  # Non-divisible endpoint: grid ends at .58, never above max.
        status,out=self.pipeline(h,config=cfg)
        self.assertEqual(status,0); self.assertEqual(out.n_frequencies,25)

        cfg.top_columns=7
        cfg.bins[4].role=cfg.bins[5].role=0  # Layout without null-check bins.
        status,out=self.pipeline(h,config=cfg)
        self.assertEqual(status,0); self.assertEqual(out.n_selected,7)
        self.assertEqual(len(set(out.selected_bins[:7])),7)
        cfg.top_columns=3

        for field,value in (("frequency_step_hz",0),("frequency_step_hz",float('nan')),
                            ("top_columns",11),("whiten_bins",6),("n_bins",12),
                            ("subcarrier_spacing_hz",0),("max_hz",.05)):
            original=getattr(cfg,field); setattr(cfg,field,value)
            self.assertNotEqual(self.pipeline(h,config=cfg)[0],0,field)
            setattr(cfg,field,original)

        cfg.bins[1].frequency_hz=cfg.bins[0].frequency_hz
        self.assertNotEqual(self.pipeline(h,config=cfg)[0],0)

if __name__ == "__main__":
    unittest.main()
