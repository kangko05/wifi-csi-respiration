"""Line-level parsing contracts."""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import support  # noqa: E402 - sibling module, tests/ is the discovery root
from csi_collect import wire  # noqa: E402


class ParseLineTest(unittest.TestCase):
    def test_valid_line_keeps_metadata_strings_verbatim(self):
        payload = bytes([0x80, 0x7F, 0x00, 0xFF, 0x01])
        line = support.csi_line(payload, seq=7, local_timestamp=4294967295, first_word=1).rstrip(b"\r\n")
        parsed = wire.parse_line(line)
        self.assertTrue(parsed.parse_ok, parsed.error)
        self.assertEqual(parsed.kind, wire.KIND_CSI)
        self.assertEqual(parsed.fields["seq"], "7")
        self.assertEqual(parsed.fields["local_timestamp"], "4294967295")
        self.assertEqual(parsed.fields["first_word"], "1")
        self.assertEqual(parsed.fields["compensate_gain"], "1.000000")
        self.assertEqual(parsed.decoded_length, len(payload))
        self.assertEqual(parsed.len_match, "true")

    def test_data_offsets_locate_the_base64_field(self):
        payload = bytes(range(64))
        line = support.csi_line(payload).rstrip(b"\r\n")
        parsed = wire.parse_line(line)
        field = line[parsed.data_rel_offset:parsed.data_rel_offset + parsed.data_length]
        self.assertEqual(base64.b64decode(field, validate=True), payload)

    def test_variable_payload_lengths_are_all_accepted(self):
        for length in (0, 2, 234, 380, 512):
            payload = bytes((i * 7) % 256 for i in range(length))
            parsed = wire.parse_line(support.csi_line(payload).rstrip(b"\r\n"))
            self.assertTrue(parsed.parse_ok, (length, parsed.error))
            self.assertEqual(parsed.decoded_length, length)

    def test_length_mismatch_is_recorded_not_dropped(self):
        parsed = wire.parse_line(support.csi_line(b"abcd", declared_len=234).rstrip(b"\r\n"))
        self.assertFalse(parsed.parse_ok)
        self.assertIn("len_mismatch:234!=4", parsed.error)
        self.assertEqual(parsed.len_match, "false")
        self.assertEqual(parsed.fields["len"], "234")
        self.assertEqual(parsed.decoded_length, 4)

    def test_base64_error_is_recorded(self):
        parsed = wire.parse_line(support.csi_line(b"", data_text="not*base64").rstrip(b"\r\n"))
        self.assertFalse(parsed.parse_ok)
        self.assertIn("base64_error", parsed.error)
        self.assertEqual(parsed.decoded_length, -1)

    def test_field_count_and_numeric_errors(self):
        short = b"CSI_DATA,1,2,3"
        self.assertIn("field_count", wire.parse_line(short).error)
        bad_numeric = support.csi_line(b"ab").replace(b"CSI_DATA,1,", b"CSI_DATA,x,").rstrip(b"\r\n")
        parsed = wire.parse_line(bad_numeric)
        self.assertIn("bad_seq", parsed.error)
        self.assertEqual(parsed.fields["seq"], "x")

    def test_nonfinite_compensate_gain_is_a_parse_error(self):
        for text in ("nan", "inf", "-inf"):
            line = support.csi_line(b"abcd").replace(b",1.000000,", f",{text},".encode("ascii")).rstrip(b"\r\n")
            parsed = wire.parse_line(line)
            self.assertFalse(parsed.parse_ok, text)
            self.assertIn("nonfinite_compensate_gain", parsed.error)
            # The original string and the payload accounting stay untouched.
            self.assertEqual(parsed.fields["compensate_gain"], text)
            self.assertEqual(parsed.decoded_length, 4)
            self.assertEqual(parsed.len_match, "true")

    def test_finite_compensate_gain_values_stay_valid(self):
        for text in ("1.000000", "0.000000", "-0.500000", "1e-3"):
            line = support.csi_line(b"abcd").replace(b",1.000000,", f",{text},".encode("ascii")).rstrip(b"\r\n")
            parsed = wire.parse_line(line)
            self.assertTrue(parsed.parse_ok, (text, parsed.error))
            self.assertEqual(parsed.fields["compensate_gain"], text)

    def test_non_ascii_csi_line(self):
        parsed = wire.parse_line(b"CSI_DATA,\xff\xfe,1")
        self.assertEqual(parsed.error, "non_ascii")
        self.assertFalse(parsed.parse_ok)

    def test_header_and_log_lines_are_classified_not_parsed_as_csi(self):
        self.assertEqual(wire.parse_line(support.HEADER_LINE.rstrip(b"\r\n")).kind, wire.KIND_HEADER)
        self.assertEqual(wire.parse_line(support.LOG_LINE.rstrip(b"\r\n")).kind, wire.KIND_OTHER)
        self.assertEqual(wire.parse_line(b"").kind, wire.KIND_OTHER)

    def test_parse_ok_does_not_claim_sample_validity(self):
        parsed = wire.parse_line(support.csi_line(b"abcd", first_word=1).rstrip(b"\r\n"))
        self.assertTrue(parsed.parse_ok)
        self.assertEqual(parsed.fields["first_word"], "1")


if __name__ == "__main__":
    unittest.main()
