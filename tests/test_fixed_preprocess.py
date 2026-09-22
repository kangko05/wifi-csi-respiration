"""Fixed Q12/Q23 preprocessing: sample error and timestamp/mask contracts."""
import ctypes as c
import math
import unittest
import numpy as np
import test_amplitude_experiments as exp
import test_amplitude_raw as raw


class FixedPreprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        exp.AmplitudeExperimentTests.setUpClass()
        cls.lib=exp.AmplitudeExperimentTests.lib
        cls.lib.csi_abs_q12.argtypes=[c.c_int8,c.c_int8]
        cls.lib.csi_abs_q12.restype=c.c_uint32
        cls.lib.test_fixed_preprocess_error.argtypes=[c.POINTER(raw.Record),c.c_size_t,
            c.c_float,c.c_float,c.POINTER(c.c_double),c.POINTER(c.c_int)]

    @classmethod
    def tearDownClass(cls):
        exp.AmplitudeExperimentTests.tearDownClass()

    def compare(self,records,rate=10,half=1):
        arr=(raw.Record*len(records))(*records)
        errors=(c.c_double*3)(); statuses=(c.c_int*2)()
        self.assertEqual(self.lib.test_fixed_preprocess_error(arr,len(records),rate,half,errors,statuses),0)
        self.assertEqual(statuses[0],statuses[1])
        return statuses[0],list(errors)

    def test_exhaustive_int8_magnitude(self):
        for re in range(-128,128):
            for im in range(-128,128):
                value=self.lib.csi_abs_q12(re,im)/4096
                self.assertLessEqual(abs(value-math.hypot(re,im)),.500001/4096)

    def test_jitter_masks_duplicates_wrap_and_rates(self):
        records=raw.RawAmplitudeTests.synthetic(self,n=2501,bins=5,jitter=True)
        records[71].timestamp=records[70].timestamp
        for i,r in enumerate(records): r.first_word_invalid=(i%41==0)
        for rate in (5,10,33,100):
            status,error=self.compare(records,rate,half=.13)
            self.assertEqual(status,0)
            self.assertGreater(error[2],0)
            self.assertLess(error[0],.001)

    def test_full_scale_random_input(self):
        records=raw.RawAmplitudeTests.synthetic(self,n=2501,bins=5,jitter=True)
        rng=np.random.default_rng(42)
        for r in records:
            for k in range(10): r.buf[k]=int(rng.integers(-128,128))
        status,error=self.compare(records)
        self.assertEqual(status,0)
        self.assertLess(error[0],.001)
        self.assertLess(error[1],.0003)

    def test_gap_and_backwards_rejected(self):
        records=raw.RawAmplitudeTests.synthetic(self,n=2501,bins=3)
        self.assertEqual(self.compare(records[:50]+records[80:])[0],1)
        records[51].timestamp=records[50].timestamp-1
        self.assertEqual(self.compare(records)[0],1)

    def test_pipeline_candidates_and_flat_input(self):
        runner=exp.AmplitudeExperimentTests()
        records=raw.RawAmplitudeTests.synthetic(self,n=6001,bins=7,jitter=True)
        for mode in (3,4):
            status,result=runner.run_case(records,single=mode)
            self.assertEqual(status,0)
            self.assertGreaterEqual(result.selected_bin,0)
            for k in range(7):
                self.assertLess(abs(result.bins[k].psd_bpm-(.25+.01*k)*60),.15)
        for r in records:
            for k in range(7):r.buf[2*k]=127;r.buf[2*k+1]=-128
        for mode in (3,4):
            status,result=runner.run_case(records,single=mode)
            self.assertEqual(status,0)
            self.assertEqual(result.selected_bin,-1)
            self.assertTrue(all(result.bins[k].reasons & 2 for k in range(7)))
