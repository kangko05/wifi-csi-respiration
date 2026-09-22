import base64
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recorded_runner", ROOT / "python/scripts/run_c_amplitude.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def line(timestamp, payload=b"\xff\x80\x01\x7f", channel=36, rssi=-29):
    return (f"CSI_DATA,1,{rssi},-94,17,16,{channel},{timestamp},47,2,{len(payload)},0,0.365,0,"
            + base64.b64encode(payload).decode()).encode()


class RunnerTests(unittest.TestCase):
    def test_signed_iq_metadata_and_wrap_preserved(self):
        text, info = runner.decode(line(4294967290)+b"\n"+line(9994))
        rows = text.splitlines()
        self.assertEqual(rows[0], "2")
        self.assertIn("1 4294967290 0 0.365 -29 -94 17 16 36 2 0 47 4 -1 -128 1 127", rows[1])
        self.assertEqual(info["wraps"], 1)
        self.assertEqual(info["input_duration_s"], .01)

    def test_bad_lines_recorded_without_time_compression(self):
        data = line(0)+b"\nCSI_DATA,broken\n"+line(10000,rssi=200)+b"\n"+line(20000)
        text, info = runner.decode(data)
        self.assertEqual(info["decoded_records"], 2)
        self.assertEqual(len(info["skipped"]), 2)
        self.assertEqual(info["input_duration_s"], .02)

    def test_backwards_and_layout_changes_rejected(self):
        with self.assertRaisesRegex(ValueError, "backwards"):
            runner.decode(line(20000)+b"\n"+line(10000))
        with self.assertRaisesRegex(ValueError, "layout"):
            runner.decode(line(0)+b"\n"+line(10000,channel=40))


if __name__ == "__main__":
    unittest.main()
