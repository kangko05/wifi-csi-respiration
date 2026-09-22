"""B0 Q15 FFT: scaled numerical contract, dynamic range, invalid inputs."""
import ctypes as c
import unittest
import numpy as np
import test_amplitude_experiments as experiments
import test_amplitude_raw as raw


class Q15FFTTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        experiments.AmplitudeExperimentTests.setUpClass()
        cls.lib = experiments.AmplitudeExperimentTests.lib
        class Plan(c.Structure):
            _fields_=[('n',c.c_size_t),('twiddles',c.POINTER(c.c_int16))]
        cls.Plan=Plan
        cls.fp=c.POINTER(c.c_float)
        cls.ip=c.POINTER(c.c_int16)
        cls.lib.csi_fft_q15_plan_init.argtypes=[c.POINTER(Plan),c.c_size_t,cls.ip,c.c_size_t]
        cls.lib.csi_fft_q15_float.argtypes=[cls.fp,cls.fp,c.c_size_t,c.POINTER(Plan),cls.ip,c.c_size_t,c.c_int]
        cls.lib.csi_fft_q15.argtypes=[cls.ip,cls.ip,c.c_size_t,c.POINTER(Plan),c.c_int,c.POINTER(c.c_uint)]
        cls.table=(c.c_int16*4096)(); cls.plan=Plan()
        assert cls.lib.csi_fft_q15_plan_init(c.byref(cls.plan),4096,cls.table,4096)==0

    @classmethod
    def tearDownClass(cls):
        experiments.AmplitudeExperimentTests.tearDownClass()

    def transform(self, values, inverse=0):
        values=np.asarray(values,dtype=np.complex64)
        re=values.real.copy(); im=values.imag.copy(); n=len(values)
        scratch=(c.c_int16*(2*n))()
        status=self.lib.csi_fft_q15_float(re.ctypes.data_as(self.fp),im.ctypes.data_as(self.fp),n,c.byref(self.plan),scratch,2*n,inverse)
        self.assertEqual(status,0)
        return re+1j*im

    def test_numpy_forward_inverse_and_scale(self):
        rng=np.random.default_rng(51)
        for n in (1,2,16,512,2048,4096):
            x=(rng.normal(size=n)+1j*rng.normal(size=n)).astype(np.complex64)
            for amplitude in (1e-20,1,1e20):
                for inverse in (0,1):
                    values=x*amplitude
                    expected=np.fft.ifft(values) if inverse else np.fft.fft(values)
                    actual=self.transform(values,inverse)
                    relative=np.linalg.norm(actual.astype(np.complex128)-expected.astype(np.complex128))/np.linalg.norm(expected.astype(np.complex128))
                    self.assertLess(relative,.003,(n,amplitude,inverse,relative))

    def test_zero_impulse_tone_and_low_energy(self):
        for n in (512,2048,4096):
            t=np.arange(n)
            signals=[np.zeros(n),np.ones(n),np.eye(1,n)[0],
                np.sin(2*np.pi*17*t/n),
                np.sin(2*np.pi*17*t/n)+.001*np.sin(2*np.pi*33*t/n)]
            for x in signals:
                expected=np.fft.fft(x)
                actual=self.transform(x)
                error=np.linalg.norm(actual-expected)/max(np.linalg.norm(expected),1)
                self.assertLess(error,.004,(n,error))
                recovered=self.transform(actual,1)
                self.assertLess(np.linalg.norm(recovered-x)/max(np.linalg.norm(x),1),.008)

    def test_core_full_scale_without_overflow(self):
        for n in (2,32,4096):
            re=np.full(n,-32768,dtype=np.int16)
            im=np.full(n,32767,dtype=np.int16)
            expected=np.fft.fft(re.astype(float)+1j*im.astype(float))
            shift=c.c_uint()
            self.assertEqual(self.lib.csi_fft_q15(re.ctypes.data_as(self.ip),im.ctypes.data_as(self.ip),n,c.byref(self.plan),0,c.byref(shift)),0)
            actual=(re.astype(float)+1j*im.astype(float))*2**shift.value
            self.assertLess(np.linalg.norm(actual-expected)/np.linalg.norm(expected),.004)

    def test_invalid_arguments_and_nonfinite(self):
        plan=self.Plan(); re=(c.c_float*8)(); im=(c.c_float*8)(); scratch=(c.c_int16*16)()
        self.assertEqual(self.lib.csi_fft_q15_plan_init(c.byref(plan),3,self.table,4096),1)
        self.assertEqual(self.lib.csi_fft_q15_plan_init(c.byref(plan),4096,self.table,4095),1)
        for n, capacity in ((3,16),(8,15),(8192,16)):
            self.assertEqual(self.lib.csi_fft_q15_float(re,im,n,c.byref(self.plan),scratch,capacity,0),1)
        re[0]=float('nan')
        self.assertEqual(self.lib.csi_fft_q15_float(re,im,8,c.byref(self.plan),scratch,16,0),1)
        self.assertEqual(self.lib.csi_fft_q15_float(re,re,8,c.byref(self.plan),scratch,16,0),1)

    def test_synthetic_pipeline(self):
        runner=experiments.AmplitudeExperimentTests()
        records=raw.RawAmplitudeTests.synthetic(self,n=6001,bins=7,jitter=True)
        a,base=runner.run_case(records,single=1)
        b,fixed=runner.run_case(records,single=2)
        self.assertEqual((a,b),(0,0))
        self.assertGreaterEqual(fixed.selected_bin,0)
        for k in range(7):
            self.assertLess(abs(base.bins[k].psd_bpm-fixed.bins[k].psd_bpm),.15)
            self.assertLess(abs(base.bins[k].acf_bpm-fixed.bins[k].acf_bpm),.15)
            self.assertLess(abs(base.bins[k].score-fixed.bins[k].score),.02)
