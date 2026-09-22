"""Experimental paths: baseline preservation and streaming equivalence."""
import ctypes as c
from pathlib import Path
import subprocess
import tempfile
import unittest
import numpy as np
from test_amplitude_raw import Record, ROOT
import test_amplitude_raw as raw_tests
from test_amplitude_reference import Result


class AmplitudeExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name)/'experiments.so'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
            '-shared', '-fPIC', '-I', str(ROOT/'include'),
            *[str(ROOT/p) for p in ('tests/amplitude_experiment_bridge.c',
            'src/amplitude.c', 'src/preprocess.c', 'src/filters.c', 'src/csi_utils.c')],
            '-lm', '-o', str(path)], check=True)
        cls.lib = c.CDLL(str(path))
        cls.lib.test_experiment.argtypes = [c.POINTER(Record), c.c_size_t,
            c.c_int, c.c_int, c.c_float, c.c_size_t, c.c_int, c.POINTER(Result)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, records, single=0, padding=4, rate=10, bins=0, stream=0):
        arr = (Record*len(records))(*records)
        result = Result()
        status = self.lib.test_experiment(arr, len(records), single, padding,
            rate, bins, stream, c.byref(result))
        return status, result

    def test_stream_matches_batch_with_jitter_duplicates_and_wrap(self):
        records = raw_tests.RawAmplitudeTests.synthetic(self, n=6001, bins=5, jitter=True)
        records[70].timestamp = records[69].timestamp
        for single in (0, 1):
            for rate in (5, 10, 100):
                a, x = self.run_case(records, single=single, rate=rate)
                b, y = self.run_case(records, single=single, rate=rate, stream=1)
                self.assertEqual((a,b),(0,0))
                self.assertEqual(bytes(x), bytes(y))

    def test_stream_rejects_masks_gap_and_backwards(self):
        records = raw_tests.RawAmplitudeTests.synthetic(self, n=2501, bins=3)
        records[50].first_word_invalid = 1
        self.assertEqual(self.run_case(records, stream=1)[0],1)
        records[50].first_word_invalid = 0
        self.assertEqual(self.run_case(records[:50]+records[70:],stream=1)[0],1)
        records[50].timestamp = records[49].timestamp-1
        self.assertEqual(self.run_case(records,stream=1)[0],1)

    def test_float_precision_and_synthetic_candidate(self):
        records = raw_tests.RawAmplitudeTests.synthetic(self, n=6001, bins=7)
        status, base = self.run_case(records)
        self.assertEqual(status,0)
        for single, padding, rate, bins in ((1,4,10,0),(0,1,10,0),
                (0,4,5,0),(0,4,10,2),(1,1,5,2)):
            status, result = self.run_case(records,single,padding,rate,bins)
            self.assertEqual(status,0)
            self.assertGreaterEqual(result.selected_bin,0)
            k=result.selected_bin
            self.assertLess(abs(result.bins[k].psd_bpm-(.25+.01*k)*60),.7)
            if single and padding==4 and rate==10:
                for j in range(7):
                    self.assertEqual(result.bins[j].accepted,base.bins[j].accepted)
                    self.assertLess(abs(result.bins[j].score-base.bins[j].score),1e-4)

    def test_float_fft_against_numpy(self):
        class Plan(c.Structure):
            _fields_=[('n',c.c_size_t),('twiddles',c.POINTER(c.c_double))]
        fp=c.POINTER(c.c_float)
        self.lib.csi_fft_plan_init.argtypes=[c.POINTER(Plan),c.c_size_t,c.POINTER(c.c_double),c.c_size_t]
        self.lib.csi_fft_float_planned.argtypes=[fp,fp,c.c_size_t,c.POINTER(Plan),c.c_int]
        table=(c.c_double*4096)(); plan=Plan()
        self.assertEqual(self.lib.csi_fft_plan_init(c.byref(plan),4096,table,4096),0)
        rng=np.random.default_rng(91)
        for n in (1,2,32,1024,4096):
            original=(rng.normal(size=n)+1j*rng.normal(size=n)).astype(np.complex64)
            re=original.real.copy(); im=original.imag.copy()
            self.assertEqual(self.lib.csi_fft_float_planned(re.ctypes.data_as(fp),im.ctypes.data_as(fp),n,c.byref(plan),0),0)
            np.testing.assert_allclose(re+1j*im,np.fft.fft(original),rtol=2e-4,atol=5e-5)
            self.assertEqual(self.lib.csi_fft_float_planned(re.ctypes.data_as(fp),im.ctypes.data_as(fp),n,c.byref(plan),1),0)
            np.testing.assert_allclose(re+1j*im,original,rtol=1e-4,atol=2e-6)
