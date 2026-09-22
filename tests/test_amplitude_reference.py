"""Run: python/.venv/Scripts/python.exe -m unittest discover -s tests -v

Requires gcc, NumPy and SciPy. Tests C numerical output against an independent
SciPy reference; synthetic data do not establish real respiration accuracy.
"""
import ctypes as c
from pathlib import Path
import subprocess
import unittest

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
FP = c.POINTER(c.c_float)
DP = c.POINTER(c.c_double)
UP = c.POINTER(c.c_uint8)


class Bin(c.Structure):
    _fields_ = [(name, c.c_int) for name in
                ("eligible", "has_psd", "has_acf", "accepted")] + [
        ("reasons", c.c_uint32)] + [(name, c.c_double) for name in
        ("psd_hz", "psd_bpm", "psd_peak", "acf_hz", "acf_bpm", "acf_peak",
         "acf_lag_s", "concentration", "score", "cycles")]


class Result(c.Structure):
    _fields_ = [(name, c.c_size_t) for name in ("n_bins", "n_samples", "n_fft")] + [
        ("start_us", c.c_uint32), ("step_us", c.c_uint64),
        ("sample_rate_hz", c.c_double), ("duration_s", c.c_double),
        ("resolution_hz", c.c_double), ("best_bin", c.c_int),
        ("selected_bin", c.c_int), ("bins", Bin * 256)]


class AmplitudeReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "build" / "amplitude_reference.dll"
        path.parent.mkdir(exist_ok=True)
        subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Wpedantic",
                        "-shared", "-I", str(ROOT / "include"),
                        str(ROOT / "tests/amplitude_bridge.c"),
                        str(ROOT / "src/filters.c"), str(ROOT / "src/csi_utils.c"),
                        "-o", str(path), "-lm"], check=True)
        cls.lib = c.CDLL(str(path))
        cls.lib.test_spectra.argtypes = [FP, c.c_size_t, c.c_double, c.c_size_t, DP, FP]
        cls.lib.test_pipeline.argtypes = [FP, UP, c.c_size_t, c.c_size_t,
                                          c.c_uint32, c.c_float, c.POINTER(Result)]
        cls.lib.test_score.argtypes = [DP, c.c_size_t, FP, c.c_size_t, c.c_double,
                                       c.POINTER(Bin)]
        cls.lib.csi_fft.argtypes = cls.lib.csi_ifft.argtypes = [FP, FP, c.c_size_t]

    def pipeline(self, values, valid=None, step=10000, half=1):
        values = np.ascontiguousarray(values, dtype=np.float32)
        if valid is None:
            valid = np.ones(values.shape, dtype=np.uint8)
        valid = np.ascontiguousarray(valid, dtype=np.uint8)
        result = Result()
        status = self.lib.test_pipeline(values.ctypes.data_as(FP), valid.ctypes.data_as(UP),
                                       len(values), values.shape[1], step, half, c.byref(result))
        return status, result

    def test_fft_inverse(self):
        rng = np.random.default_rng(123)
        for n in (1, 2, 64, 2048):
            re = rng.normal(size=n).astype(np.float32)
            im = rng.normal(size=n).astype(np.float32)
            original = re.astype(float) + 1j * im.astype(float)
            self.assertEqual(self.lib.csi_fft(re.ctypes.data_as(FP), im.ctypes.data_as(FP), n), 0)
            np.testing.assert_allclose(re+1j*im, np.fft.fft(original), atol=4e-5, rtol=1e-5)
            self.assertEqual(self.lib.csi_ifft(re.ctypes.data_as(FP), im.ctypes.data_as(FP), n), 0)
            np.testing.assert_allclose(re+1j*im, original, atol=1e-6, rtol=1e-5)

    def test_psd_density_and_linear_acf(self):
        rng = np.random.default_rng(37)
        for n in (7, 80, 581):
            x = signal.detrend(rng.normal(size=n)).astype(np.float32)
            n_fft = 1 << (4*n-1).bit_length()
            psd = np.empty(n_fft//2+1)
            acf = np.empty(n, dtype=np.float32)
            status = self.lib.test_spectra(x.ctypes.data_as(FP), n, 10, n_fft,
                                           psd.ctypes.data_as(DP), acf.ctypes.data_as(FP))
            self.assertEqual(status, 0)
            _, expected = signal.periodogram(x.astype(float), fs=10, window="hann",
                                             nfft=n_fft, detrend=False, scaling="density")
            np.testing.assert_allclose(psd, expected, atol=2e-7, rtol=3e-5)
            correlation = np.correlate(x.astype(float), x.astype(float), mode="full")[n-1:]
            np.testing.assert_allclose(acf, correlation/correlation[0], atol=2e-7, rtol=3e-5)
            # Parseval including correct DC/Nyquist one-sided factors.
            w = signal.windows.hann(n, sym=False)
            self.assertAlmostEqual(psd.sum()*10/n_fft, np.sum((x*w)**2)/np.sum(w*w), places=6)

    def test_pipeline_reference_and_candidate(self):
        # 50 Hz acquisition -> 100 Hz interpolation -> FIR -> 10 Hz -> spectra.
        t = np.arange(3001)/50
        values = np.column_stack((10+np.sin(2*np.pi*.25*t), np.full(len(t), 10), np.ones(len(t))))
        valid = np.ones(values.shape, dtype=np.uint8)
        valid[:, 2] = 0
        status, result = self.pipeline(values, valid, step=20000)
        self.assertEqual(status, 0)
        self.assertEqual(result.selected_bin, 0)
        self.assertEqual(result.bins[1].reasons, 2)  # flat
        self.assertEqual(result.bins[2].reasons, 1)  # excluded
        grid = np.arange(6001)/100
        regular = np.interp(grid, t, values[:,0].astype(np.float32))
        taps = signal.firwin(201, 4, fs=100, window=("kaiser",8))
        reduced = signal.resample_poly(regular, 1, 10, window=taps, padtype="line")[10:-10]
        x = signal.detrend(reduced)
        x -= x.mean()
        frequencies, psd = signal.periodogram(x, fs=10, window="hann", nfft=result.n_fft,
                                             detrend=False, scaling="density")
        band = np.flatnonzero((frequencies>=.05)&(frequencies<=.8))
        peak = band[np.argmax(psd[band])]
        acf = np.correlate(x,x,"full")[len(x)-1:]
        acf /= acf[0]
        peaks, _ = signal.find_peaks(acf)
        peaks = peaks[(peaks/10>=1/.8)&(peaks/10<=min(1/.05,(len(x)-1)/20))&(acf[peaks]>0)]
        best = peaks[np.argmax(acf[peaks])]
        curvature = acf[best-1]-2*acf[best]+acf[best+1]
        offset = np.clip(.5*(acf[best-1]-acf[best+1])/curvature,-.5,.5)
        b = result.bins[0]
        self.assertEqual(result.n_samples, len(x))
        self.assertEqual((result.start_us,result.step_us),(1000000,100000))
        self.assertAlmostEqual(b.psd_hz, frequencies[peak], places=10)
        self.assertAlmostEqual(b.acf_hz, 10/(best+offset), places=5)
        self.assertAlmostEqual(b.acf_peak, acf[best], places=5)
        np.testing.assert_allclose(b.psd_peak,psd[peak],rtol=1e-5)
        neighborhood = (abs(frequencies-frequencies[peak])<=10/len(x))&(frequencies>0)
        self.assertAlmostEqual(b.concentration,psd[neighborhood].sum()/psd[1:].sum(),places=5)

    def test_short_flat_invalid_and_no_retention(self):
        status, result = self.pipeline(np.full((3001,1),10))
        self.assertEqual(status,0)
        self.assertEqual(result.selected_bin,-1)
        self.assertFalse(result.bins[0].has_psd)
        t=np.arange(1001)/100
        status,result=self.pipeline((10+np.sin(2*np.pi*.25*t))[:,None])
        self.assertEqual(status,0)
        self.assertEqual(result.selected_bin,-1)
        self.assertTrue(result.bins[0].reasons & 4)  # insufficient duration
        self.assertEqual(self.pipeline(np.ones((2,1)))[0],1)
        x=np.ones((3001,1)); mask=np.zeros(x.shape,dtype=np.uint8)
        self.assertEqual(self.pipeline(x,mask)[0],1)
        x[1500]=np.nan
        self.assertEqual(self.pipeline(x)[0],1)

    def test_score_disagreement_and_plateau(self):
        n,nfft,fs=600,4096,10
        freq=np.arange(nfft//2+1)*fs/nfft
        psd=np.exp(-((freq-.25)/.005)**2)
        acf=np.zeros(n,dtype=np.float32)
        acf[0]=1
        acf[20]=.8  # .5Hz, disagrees with .25Hz PSD
        b=Bin()
        self.lib.test_score(psd.ctypes.data_as(DP),nfft,acf.ctypes.data_as(FP),n,fs,c.byref(b))
        self.assertTrue(b.reasons & 128)
        self.assertFalse(b.accepted)
        acf[20]=0
        acf[39:42]=.8  # plateau center is lag 40
        self.lib.test_score(psd.ctypes.data_as(DP),nfft,acf.ctypes.data_as(FP),n,fs,c.byref(b))
        self.assertAlmostEqual(b.acf_hz,.25)
        self.assertTrue(b.accepted)


if __name__ == "__main__":
    unittest.main()
