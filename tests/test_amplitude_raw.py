"""Raw-reader path vs unchanged full-frame amplitude path, all result fields."""
import base64
import ctypes as c
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from test_amplitude_reference import Result

ROOT = Path(__file__).resolve().parents[1]


class Record(c.Structure):
    _fields_ = [(n, c.c_uint32) for n in ('seq', 'timestamp', 'dropped')] + [
        ('compensate_gain', c.c_float), ('rssi', c.c_int8),
        ('noise_floor', c.c_int8), ('fft_gain', c.c_int8),
        ('agc_gain', c.c_uint8), ('channel', c.c_uint8),
        ('bb_format', c.c_uint8), ('first_word_invalid', c.c_uint8),
        ('sig_len', c.c_uint16), ('len', c.c_uint16), ('buf', c.c_int8 * 512)]


class RawAmplitudeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='amp-raw-')
        libpath = Path(cls.temp.name) / 'raw.so'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', '-I', str(ROOT/'include'),
                        *[str(ROOT/p) for p in ('tests/amplitude_raw_bridge.c',
                          'src/amplitude.c', 'src/preprocess.c', 'src/filters.c',
                          'src/csi_utils.c')], '-lm', '-Wl,--wrap=malloc', '-Wl,--wrap=free', '-o', str(libpath)], check=True)
        cls.lib = c.CDLL(str(libpath))
        cls.lib.test_raw_peak.restype = c.c_size_t
        cls.lib.test_raw_live.restype = c.c_size_t
        cls.lib.test_raw_parity.argtypes = [c.POINTER(Record), c.c_size_t,
            c.c_size_t, c.c_float, c.c_float, c.POINTER(Result),
            c.POINTER(Result), c.POINTER(c.c_int), c.POINTER(c.c_int)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compare(self, records, head=0, target_fs=10, half=1):
        n = len(records)
        ring = (Record * n)()
        for i, r in enumerate(records):
            ring[(head+i) % n] = r
        old, new = Result(), Result()
        a, b = c.c_int(), c.c_int()
        self.assertEqual(self.lib.test_raw_parity(ring, n, head, target_fs,
                         half, c.byref(old), c.byref(new), c.byref(a), c.byref(b)), 0)
        self.assertEqual(self.lib.test_raw_live(), 0)
        self.assertEqual(a.value, b.value)
        if a.value == 0:
            # Includes every bin, score, flag, PSD/ACF result and summary field.
            self.assertEqual(bytes(old), bytes(new), 'non-identical result')
        return a.value, new

    def synthetic(self, n=6001, bins=7, jitter=False):
        rng = np.random.default_rng(4)
        records=[]
        for i in range(n):
            t=i*10000 + (int(rng.integers(-500, 500)) if jitter and i else 0)
            r=Record(seq=i, timestamp=(4294000000+t) % 2**32,
                     compensate_gain=0.25+(i % 8), len=bins*2)
            for k in range(bins):
                r.buf[2*k]=3
                r.buf[2*k+1]=round(40+20*np.sin(2*np.pi*(.25+.01*k)*t/1e6))
            records.append(r)
        return records

    def test_synthetic_wrap_ring_jitter_masks_and_gain(self):
        records=self.synthetic(jitter=True)
        for i,r in enumerate(records):
            r.first_word_invalid = i % 40 == 0 or i in (0, len(records)-1)
        status,result=self.compare(records, head=521)
        self.assertEqual(status,0)
        self.assertGreaterEqual(result.selected_bin,0)

    def test_flat_short_gap_duplicates_and_excluded(self):
        records=self.synthetic(n=2501, bins=3)
        for r in records:
            r.first_word_invalid=1
            r.buf[4]=0; r.buf[5]=20
        status,result=self.compare(records)
        self.assertEqual(status,0)
        self.assertEqual(result.selected_bin,-1)
        self.assertEqual(result.bins[0].reasons,1)
        self.assertEqual(result.bins[2].reasons,2)
        self.compare(records[:1001])
        self.assertEqual(self.compare(records[:2])[0],1)
        self.assertEqual(self.compare(records[:100]+records[130:])[0],1)
        records[70].timestamp=records[69].timestamp
        self.compare(records)
        records[70].timestamp=records[69].timestamp-1
        self.assertEqual(self.compare(records)[0],1)

    def test_downsample_alignment_and_passthrough(self):
        records=self.synthetic(n=2501,bins=3)
        for rate in (10, 33, 100):
            with self.subTest(rate=rate):
                self.compare(records,head=13,target_fs=rate,half=.13)

    def test_memory_bound_and_allocation_failures(self):
        records=self.synthetic(bins=117)
        self.compare(records)
        self.assertEqual(self.lib.test_raw_peak(), 77812)
        array=(Record*len(records))(*records)
        for index in range(6):
            self.lib.test_raw_fail_at(index)
            old,new=Result(),Result()
            a,b=c.c_int(),c.c_int()
            self.assertEqual(self.lib.test_raw_parity(array,len(records),0,10,1,
                c.byref(old),c.byref(new),c.byref(a),c.byref(b)),0)
            self.assertEqual(a.value,0)
            self.assertEqual(b.value,1)
            self.assertEqual(self.lib.test_raw_live(),0)
        self.lib.test_raw_fail_at(-1)

    def test_fixed_preprocess_allocation_failures(self):
        records=self.synthetic(n=2501,bins=3)
        array=(Record*len(records))(*records)
        self.lib.test_fixed_allocation_failure.argtypes=[c.POINTER(Record),c.c_size_t,c.c_int]
        for index in range(8):
            self.assertEqual(self.lib.test_fixed_allocation_failure(array,len(records),index),1)
            self.assertEqual(self.lib.test_raw_live(),0)
        self.assertEqual(self.lib.test_fixed_allocation_failure(array,len(records),-1),0)

    def test_fft_cache_forward_inverse_and_validation(self):
        class Plan(c.Structure):
            _fields_ = [('n', c.c_size_t), ('twiddles', c.POINTER(c.c_double))]
        fp = c.POINTER(c.c_float)
        self.lib.csi_fft_plan_init.argtypes = [c.POINTER(Plan), c.c_size_t,
                                             c.POINTER(c.c_double), c.c_size_t]
        for name in ('csi_fft_planned', 'csi_ifft_planned'):
            getattr(self.lib, name).argtypes = [fp, fp, c.c_size_t, c.POINTER(Plan)]
        for name in ('csi_fft', 'csi_ifft'):
            getattr(self.lib, name).argtypes = [fp, fp, c.c_size_t]
        table = (c.c_double * 4096)()
        plan = Plan()
        self.assertEqual(self.lib.csi_fft_plan_init(c.byref(plan), 4096, table, 4095), 1)
        self.assertEqual(self.lib.csi_fft_plan_init(c.byref(plan), 3, table, 4096), 1)
        self.assertEqual(self.lib.csi_fft_plan_init(c.byref(plan), 4096, table, 4096), 0)
        original_table = bytes(table)
        rng = np.random.default_rng(77)
        for n in (1, 2, 16, 512, 2048, 4096):
            for inverse in (False, True):
                re = rng.normal(size=n).astype(np.float32)
                im = rng.normal(size=n).astype(np.float32)
                a, b = re.copy(), im.copy()
                plain = self.lib.csi_ifft if inverse else self.lib.csi_fft
                cached = self.lib.csi_ifft_planned if inverse else self.lib.csi_fft_planned
                self.assertEqual(plain(re.ctypes.data_as(fp), im.ctypes.data_as(fp), n), 0)
                self.assertEqual(cached(a.ctypes.data_as(fp), b.ctypes.data_as(fp), n, c.byref(plan)), 0)
                self.assertEqual(re.tobytes(), a.tobytes())
                self.assertEqual(im.tobytes(), b.tobytes())
        self.assertEqual(bytes(table), original_table)
        self.assertEqual(self.lib.csi_fft_planned(a.ctypes.data_as(fp), b.ctypes.data_as(fp), 8192, c.byref(plan)), 1)
        self.assertEqual(self.lib.csi_fft_planned(a.ctypes.data_as(fp), b.ctypes.data_as(fp), 4096, None), 1)

    def test_recorded_sessions(self):
        paths=sorted((ROOT/'data').glob('*/serial.bin'))
        if not paths:
            self.skipTest('no local captures')
        for path in paths:
            records=[]
            for line in path.read_bytes().splitlines():
                if not line.startswith(b'CSI_DATA,'): continue
                parts=line.split(b',')
                if len(parts)!=15: continue
                try:
                    data=base64.b64decode(parts[14],validate=True)
                    r=Record(seq=int(parts[1]),rssi=int(parts[2]),
                        noise_floor=int(parts[3]),fft_gain=int(parts[4]),
                        agc_gain=int(parts[5]),channel=int(parts[6]),
                        timestamp=int(parts[7]),sig_len=int(parts[8]),
                        bb_format=int(parts[9]),len=int(parts[10]),
                        first_word_invalid=int(parts[11]),
                        compensate_gain=float(parts[12]),dropped=int(parts[13]))
                    if not len(data)==r.len or not 0<r.len<=512 or r.len%2: continue
                    c.memmove(r.buf,data,len(data))
                    records.append(r)
                except (ValueError, OverflowError):
                    continue
            with self.subTest(session=path.parent.name,frames=len(records)):
                self.assertGreater(len(records),1)
                self.compare(records,head=37 % len(records))
                # Actual 60-second sliding windows, including a later ring position.
                for end in (min(len(records),6001),len(records)):
                    self.compare(records[max(0,end-6000):end],head=19)
