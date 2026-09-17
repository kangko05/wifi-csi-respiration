"""Adapter contracts: signed I/Q, mappings, device time, exclusions, preservation.

Inputs are produced by the real collector (`csi_collect.recorder`) driven by the
offline fake serial stream, so the adapter is tested against genuinely
collector-shaped `serial.bin`/`lines.csv`/`session.json`, not a hand-written
imitation of them.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

import support  # noqa: E402 - sibling module, tests/ is the discovery root
from csi_adapt import legacy_format  # noqa: E402
from csi_collect import recorder  # noqa: E402

TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-tests"
CAPTURE_BYTES = legacy_format.CAPTURE_BYTES
UINT32 = legacy_format.UINT32


def payload(seed: int) -> bytes:
    """234 bytes covering the full signed int8 range, including -128 and -1."""
    values = [((seed * 7 + index * 13) % 256) - 128 for index in range(CAPTURE_BYTES)]
    values[0], values[1], values[2], values[3] = -128, 127, -1, 0
    return bytes(value & 0xFF for value in values)


def csi_line(data: bytes, *, seq: int, local_timestamp: int, rx_format: int = 2,
             declared_len: int | None = None, first_word: int = 0) -> bytes:
    fields = ["CSI_DATA", str(seq), "-29", "-94", "18", "16", "36", str(local_timestamp),
              "47", str(rx_format), str(len(data) if declared_len is None else declared_len),
              str(first_word), "0.354813", "0",
              base64.b64encode(data).decode("ascii")]
    return ",".join(fields).encode("ascii") + b"\r\n"


class CaptureMixin:
    """Shared helpers; the test bodies live in exactly one TestCase each."""

    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def capture(self, stream_bytes: bytes) -> Path:
        """Record one capture through the real collector and return its directory."""
        clock = support.FakeClock()
        serial = support.FakeSerial(clock, [(0.0, stream_bytes)])
        session = recorder.create_session(
            self.root / "data",
            config={"port": "FAKE", "baud": 921600},
            conditions={"location": "교수회의실", "occupancy": "1", "distance_cm": 100.0,
                        "posture": "sitted", "notes": None},
            host={"command_argv": ["collect.py", "--port", "FAKE"]},
            utcnow=clock.utcnow, monotonic=clock.monotonic, notify=lambda _: None,
        )
        session.record(serial, 1.0, 5.0)
        return session.path

    def convert(self, source: Path, name: str = "derived", **kwargs):
        return legacy_format.convert_session(source, self.root / name, **kwargs)

    @staticmethod
    def rows(path: Path) -> list[dict]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def tree_hashes(root: Path) -> dict[str, str]:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def stream(self, n: int = 6, *, start_us: int = 1_000_000, step_us: int = 10_000,
               start_seq: int = 100) -> bytes:
        return b"".join(csi_line(payload(i), seq=start_seq + i,
                                 local_timestamp=start_us + i * step_us) for i in range(n))


class AdapterTestCase(CaptureMixin, unittest.TestCase):
    # -- signed I/Q and byte identity -----------------------------------
    def test_signed_int8_iq_bytes_survive_the_round_trip(self):
        source = self.capture(self.stream())
        self.convert(source)
        array = np.load(self.root / "derived/csi_raw.npy")
        self.assertEqual(array.dtype, np.int8)
        self.assertEqual(array.shape, (6, CAPTURE_BYTES))
        for index in range(6):
            expected = np.frombuffer(payload(index), dtype=np.int8)
            np.testing.assert_array_equal(array[index], expected)
        # Negative values are preserved as signed, not folded into 0..255.
        self.assertEqual(array[0, 0], -128)
        self.assertEqual(array[0, 1], 127)
        self.assertEqual(array[0, 2], -1)
        # Byte order untouched: even bytes are imaginary, odd bytes are real.
        csi = array[:, 1::2].astype(np.complex64)
        csi.imag = array[:, 0::2]
        self.assertEqual(csi.shape, (6, CAPTURE_BYTES // 2))
        self.assertEqual(csi[0, 0], complex(127, -128))

    def test_rowmap_recovers_original_line_and_byte_offsets(self):
        source = self.capture(self.stream())
        self.convert(source)
        raw = (source / "serial.bin").read_bytes()
        lines = self.rows(source / "lines.csv")
        mapping = self.rows(self.root / "derived/rowmap.csv")
        array = np.load(self.root / "derived/csi_raw.npy")
        self.assertEqual(len(mapping), len(array))
        for index, row in enumerate(mapping):
            self.assertEqual(int(row["packet_index"]), index)
            line = lines[int(row["source_line_index"])]
            self.assertEqual(row["source_line_byte_offset"], line["byte_offset"])
            self.assertEqual(row["host_elapsed_s"], line["host_elapsed_s"])
            self.assertEqual(row["host_utc"], line["host_utc"])
            offset, length = int(row["source_data_byte_offset"]), int(row["source_data_byte_length"])
            decoded = base64.b64decode(raw[offset:offset + length], validate=True)
            np.testing.assert_array_equal(np.frombuffer(decoded, dtype=np.int8), array[index])

    def test_meta_rows_align_with_packets_and_keep_wire_strings(self):
        source = self.capture(self.stream())
        report = self.convert(source)
        meta = self.rows(self.root / "derived/meta.csv")
        lines = [row for row in self.rows(source / "lines.csv") if row["kind"] == "csi"]
        self.assertEqual(len(meta), len(lines))
        for derived_row, line in zip(meta, lines):
            for name in ("seq", "rssi", "noise_floor", "fft_gain", "agc_gain", "channel",
                         "sig_len", "rx_format", "len", "first_word", "compensate_gain", "dropped"):
                self.assertEqual(derived_row[name], line[name])
            self.assertEqual(derived_row["local_timestamp"], line["local_timestamp"])
        info = json.loads((self.root / "derived/session.json").read_text(encoding="utf-8"))
        self.assertEqual(info["n_records"], len(meta))
        self.assertEqual(info["n_kept"], len(meta))
        self.assertEqual(info["csi_bytes"], CAPTURE_BYTES)
        self.assertEqual(info["n_bins"], CAPTURE_BYTES // 2)
        self.assertEqual(report.n_kept_packets, len(meta))

    # -- device time -----------------------------------------------------
    def test_strictly_increasing_device_time_is_copied_verbatim(self):
        source = self.capture(self.stream())
        report = self.convert(source)
        self.assertEqual(report.timestamp_policy["policy"], "verbatim")
        self.assertEqual(report.timestamp_policy["wraps"], [])
        meta = self.rows(self.root / "derived/meta.csv")
        self.assertEqual([row["local_timestamp"] for row in meta],
                         [str(1_000_000 + i * 10_000) for i in range(6)])

    def test_irregular_gaps_are_preserved_not_resampled(self):
        offsets = [0, 10_000, 250_000, 260_000, 270_000]
        data = b"".join(csi_line(payload(i), seq=i, local_timestamp=1_000_000 + offset)
                        for i, offset in enumerate(offsets))
        source = self.capture(data)
        self.convert(source)
        meta = self.rows(self.root / "derived/meta.csv")
        values = [int(row["local_timestamp"]) for row in meta]
        self.assertEqual(len(values), len(offsets))
        self.assertEqual(np.diff(values).tolist(), np.diff(offsets).tolist())

    def test_uint32_wrap_is_unwrapped_rebased_and_logged(self):
        base = UINT32 - 25_000
        raw_times = [(base + i * 10_000) % UINT32 for i in range(6)]
        self.assertLess(raw_times[3], raw_times[2])  # the capture really wraps
        data = b"".join(csi_line(payload(i), seq=i, local_timestamp=t)
                        for i, t in enumerate(raw_times))
        source = self.capture(data)
        report = self.convert(source)
        policy = report.timestamp_policy
        self.assertEqual(policy["policy"], "unwrap_uint32_rebased")
        self.assertEqual(len(policy["wraps"]), 1)
        self.assertEqual(policy["wraps"][0]["packet_index"], 3)
        self.assertEqual(policy["wraps"][0]["implied_interval_us"], 10_000)
        self.assertEqual(policy["rebase_subtracted_us"], base)
        emitted = [int(row["local_timestamp"]) for row in self.rows(self.root / "derived/meta.csv")]
        self.assertEqual(emitted, [i * 10_000 for i in range(6)])
        self.assertTrue(all(0 <= value < UINT32 for value in emitted))
        # Original device values remain recoverable from the sidecar mapping.
        mapping = self.rows(self.root / "derived/rowmap.csv")
        self.assertEqual([int(r["device_local_timestamp_us"]) for r in mapping], raw_times)

    def test_non_wrap_backwards_time_is_refused(self):
        times = [1_000_000, 1_010_000, 1_005_000, 1_020_000]
        data = b"".join(csi_line(payload(i), seq=i, local_timestamp=t)
                        for i, t in enumerate(times))
        source = self.capture(data)
        with self.assertRaises(legacy_format.TimestampPolicyError):
            self.convert(source)

    def test_duplicate_device_time_is_refused(self):
        times = [1_000_000, 1_010_000, 1_010_000, 1_020_000]
        data = b"".join(csi_line(payload(i), seq=i, local_timestamp=t)
                        for i, t in enumerate(times))
        source = self.capture(data)
        with self.assertRaises(legacy_format.TimestampPolicyError):
            self.convert(source)

    def test_resolve_timestamps_preserves_every_difference(self):
        values = [(UINT32 - 30_000 + i * 9_999) % UINT32 for i in range(12)]
        emitted, policy = legacy_format.resolve_timestamps(values)
        self.assertEqual(policy["policy"], "unwrap_uint32_rebased")
        expected = [(value - values[0]) % UINT32 for value in values]
        self.assertEqual(emitted, expected)
        self.assertTrue(all(b - a == 9_999 for a, b in zip(emitted, emitted[1:])))

    # -- repeats, exclusions, invalid input ------------------------------
    def test_repeated_seq_values_are_kept_unmodified(self):
        data = (csi_line(payload(0), seq=500, local_timestamp=1_000_000)
                + csi_line(payload(1), seq=501, local_timestamp=1_010_000)
                + csi_line(payload(2), seq=500, local_timestamp=1_020_000))
        source = self.capture(data)
        report = self.convert(source)
        meta = self.rows(self.root / "derived/meta.csv")
        self.assertEqual([row["seq"] for row in meta], ["500", "501", "500"])
        self.assertEqual(len(report.seq_repeats), 1)
        self.assertEqual(report.seq_repeats[0]["seq"], 500)
        self.assertFalse(report.seq_repeats[0]["identical_payload"])
        array = np.load(self.root / "derived/csi_raw.npy")
        np.testing.assert_array_equal(array[2], np.frombuffer(payload(2), dtype=np.int8))

    def test_malformed_first_row_is_excluded_and_logged_not_zero_filled(self):
        broken = b"CSI_DATA,1,2,3,4,5,6,7,8,9,10,11,12,13\r\n"  # field_count:14, as seen in data/
        data = broken + self.stream(n=3)
        source = self.capture(data)
        report = self.convert(source)
        self.assertEqual(report.n_kept_packets, 3)
        self.assertEqual(report.n_excluded_rows, 1)
        excluded = self.rows(self.root / "derived/excluded_rows.csv")
        self.assertEqual(len(excluded), 1)
        self.assertEqual(int(excluded[0]["source_line_index"]), 0)
        self.assertEqual(int(excluded[0]["source_line_byte_offset"]), 0)
        self.assertIn("field_count:14", excluded[0]["wire_error"])
        self.assertIn("field_count:14", excluded[0]["exclusion_reason"])
        array = np.load(self.root / "derived/csi_raw.npy")
        self.assertEqual(len(array), 3)
        self.assertFalse((array == 0).all(axis=1).any())  # no malformed row became valid zeros
        np.testing.assert_array_equal(array[0], np.frombuffer(payload(0), dtype=np.int8))

    def test_partial_leading_line_and_trailing_bytes_are_excluded_and_recorded(self):
        data = b"lt_2*z\r\n" + self.stream(n=3) + b"CSI_DATA,9,"
        source = self.capture(data)
        report = self.convert(source)
        self.assertEqual(report.n_kept_packets, 3)
        reasons = [row["exclusion_reason"] for row in
                   self.rows(self.root / "derived/excluded_rows.csv")]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any(reason == "not_csi_line:other" for reason in reasons))
        self.assertTrue(any(reason == "not_csi_line:trailing_partial" for reason in reasons))
        info = json.loads((source / "session.json").read_text(encoding="utf-8"))
        self.assertGreater(info["counters"]["trailing_partial_bytes"], 0)

    def test_declared_length_mismatch_is_excluded(self):
        data = (self.stream(n=2)
                + csi_line(payload(9), seq=9, local_timestamp=1_030_000, declared_len=200))
        source = self.capture(data)
        report = self.convert(source)
        self.assertEqual(report.n_kept_packets, 2)
        self.assertEqual(report.n_excluded_rows, 1)

    def test_unexpected_rx_format_is_excluded(self):
        data = (self.stream(n=2)
                + csi_line(payload(9), seq=9, local_timestamp=1_030_000, rx_format=3))
        source = self.capture(data)
        self.assertEqual(self.convert(source).n_kept_packets, 2)

    def test_tampered_serial_bin_is_refused(self):
        source = self.capture(self.stream())
        raw = bytearray((source / "serial.bin").read_bytes())
        raw[100] ^= 0x01
        (source / "serial.bin").write_bytes(bytes(raw))
        with self.assertRaises(legacy_format.ConversionError):
            self.convert(source)

    # -- preservation and annotations ------------------------------------
    def test_original_capture_is_untouched_by_conversion(self):
        source = self.capture(self.stream())
        before = self.tree_hashes(source)
        self.convert(source)
        self.assertEqual(self.tree_hashes(source), before)

    def test_conversion_refuses_to_write_inside_the_original(self):
        source = self.capture(self.stream())
        with self.assertRaises(legacy_format.ConversionError):
            legacy_format.convert_session(source, source / "derived")
        with self.assertRaises(FileExistsError):
            legacy_format.convert_session(source, source.parent)

    def test_interrupted_annotation_lands_in_derived_json_only(self):
        source = self.capture(self.stream())
        before = self.tree_hashes(source)
        annotations = {"operator_interrupted": True, "source_manifest_status": "complete",
                       "note": "user confirmed interruption", "notes": ["analyse received span only"]}
        report = self.convert(source, annotations=annotations)
        derived = json.loads((self.root / "derived/session.json").read_text(encoding="utf-8"))
        self.assertTrue(derived["annotations"]["operator_interrupted"])
        self.assertEqual(derived["annotations"]["source_manifest_status"], "complete")
        self.assertEqual(derived["derived_from"]["source_status"], "complete")
        self.assertEqual(report.notes, ["analyse received span only"])
        self.assertEqual(self.tree_hashes(source), before)
        original = json.loads((source / "session.json").read_text(encoding="utf-8"))
        self.assertNotIn("annotations", original)
        self.assertEqual(original["status"], "complete")

    def test_derived_json_carries_provenance_and_no_labels(self):
        source = self.capture(self.stream())
        report = self.convert(source)
        derived = json.loads((self.root / "derived/session.json").read_text(encoding="utf-8"))
        original = json.loads((source / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(derived["derived_from"]["source_raw_sha256"], original["raw_sha256"])
        self.assertEqual(derived["derived_from"]["source_conditions"], original["conditions"])
        self.assertFalse(derived["labels"]["manual_reference_recorded"])
        self.assertFalse(derived["interpretation"]["resampled"])
        self.assertFalse(derived["interpretation"]["deduplicated"])
        self.assertEqual(report.source_sha256["serial.bin"],
                         hashlib.sha256((source / "serial.bin").read_bytes()).hexdigest())
        self.assertEqual(
            report.derived_sha256["csi_raw.npy"],
            hashlib.sha256((self.root / "derived/csi_raw.npy").read_bytes()).hexdigest())


class LegacyReaderIntegrationTestCase(CaptureMixin, unittest.TestCase):
    """The derived trio must satisfy the legacy reader's own input contract."""

    LEGACY_SRC = Path(__file__).resolve().parents[1] / "vendor" / "wifi-csi-proto" / "src"

    def setUp(self):
        super().setUp()
        if not (self.LEGACY_SRC / "csi_pipeline" / "input.py").exists():
            self.skipTest("legacy checkout not available")
        sys.dont_write_bytecode = True
        self.addCleanup(setattr, sys, "dont_write_bytecode", False)
        if str(self.LEGACY_SRC) not in sys.path:
            sys.path.insert(0, str(self.LEGACY_SRC))
        try:
            from csi_pipeline import load_session
        except ImportError:  # pragma: no cover - environment dependent
            self.skipTest("legacy csi_pipeline not importable")
        self.load_session = load_session

    def test_legacy_loader_reads_derived_session_with_original_values(self):
        source = self.capture(self.stream())
        self.convert(source)
        session = self.load_session(self.root / "derived")
        self.assertEqual(session.csi.shape, (6, CAPTURE_BYTES // 2))
        np.testing.assert_allclose(session.time_s, np.arange(6) * 0.01)
        np.testing.assert_array_equal(
            session.metadata["local_timestamp"],
            np.array([1_000_000 + i * 10_000 for i in range(6)]))
        np.testing.assert_array_equal(session.raw_csi[0],
                                      np.frombuffer(payload(0), dtype=np.int8))
        self.assertEqual(session.csi[0, 0], complex(127, -128))
        self.assertTrue((session.metadata["rx_format"] == 2).all())

    def test_legacy_loader_reads_a_wrapped_capture_after_the_logged_policy(self):
        base = UINT32 - 25_000
        raw_times = [(base + i * 10_000) % UINT32 for i in range(6)]
        data = b"".join(csi_line(payload(i), seq=i, local_timestamp=t)
                        for i, t in enumerate(raw_times))
        source = self.capture(data)
        self.convert(source)
        session = self.load_session(self.root / "derived")
        np.testing.assert_allclose(session.time_s, np.arange(6) * 0.01)


if __name__ == "__main__":
    unittest.main()
