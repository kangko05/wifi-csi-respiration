"""Session recorder contracts: raw preservation, indexing, timing and statuses."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import support  # noqa: E402 - sibling module, tests/ is the discovery root
from csi_collect import recorder  # noqa: E402

TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-tests"


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "data"
        self.messages: list[str] = []

    def make_session(self, clock, conditions=None):
        return recorder.create_session(
            self.root,
            config={"port": "FAKE", "baud": 921600},
            conditions=conditions or {"location": "옆 교수회의실", "occupancy": None, "notes": None},
            host={"command_argv": ["collect.py", "--port", "FAKE"]},
            utcnow=clock.utcnow,
            monotonic=clock.monotonic,
            notify=self.messages.append,
        )

    def run_session(self, events, duration=5.0, idle_timeout=30.0, conditions=None, clock=None, chunk_size=None):
        clock = clock or support.FakeClock()
        stream = support.FakeSerial(clock, events, chunk_size=chunk_size)
        session = self.make_session(clock, conditions)
        info = session.record(stream, duration, idle_timeout)
        return session, info

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def read_rows(path: Path) -> list[dict]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def read_session(self, session) -> tuple[bytes, list[dict], list[dict], dict]:
        raw = (session.path / "serial.bin").read_bytes()
        lines = self.read_rows(session.path / "lines.csv")
        chunks = self.read_rows(session.path / "chunks.csv")
        info = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
        return raw, lines, chunks, info


class RawPreservationTest(RecorderTestCase):
    def test_raw_bytes_offsets_and_line_alignment(self):
        stream_bytes = (
            support.LOG_LINE
            + support.HEADER_LINE
            + support.csi_line(bytes(range(32)), seq=1)
            + support.csi_line(bytes(range(64)), seq=2)
        )
        # Split at arbitrary points, including in the middle of a CSI line.
        events = [(0.0, stream_bytes[:20]), (0.1, stream_bytes[20:130]), (0.2, stream_bytes[130:])]
        session, info = self.run_session(events)
        raw, lines, chunks, info = self.read_session(session)

        self.assertEqual(raw, stream_bytes)
        self.assertEqual(info["counters"]["raw_bytes"], len(stream_bytes))
        # chunks.csv reconstructs the raw stream exactly, in order.
        rebuilt = b"".join(
            raw[int(c["byte_offset"]):int(c["byte_offset"]) + int(c["byte_length"])] for c in chunks
        )
        self.assertEqual(rebuilt, stream_bytes)
        # lines.csv slices cover the stream with no gaps and no overlaps.
        cursor = 0
        for row in lines:
            self.assertEqual(int(row["byte_offset"]), cursor)
            content = raw[cursor:cursor + int(row["byte_length"])]
            self.assertNotIn(b"\n", content)
            cursor += int(row["byte_length"]) + int(row["terminator_length"])
        self.assertEqual(cursor, len(stream_bytes))
        self.assertEqual(info["counters"]["trailing_partial_bytes"], 0)
        self.assertEqual([r["kind"] for r in lines], ["other", "header", "csi", "csi"])
        self.assertEqual([r["terminator"] for r in lines], ["crlf"] * 4)
        self.assertEqual(info["raw_sha256"], hashlib.sha256(stream_bytes).hexdigest())

    def test_signed_edge_bytes_and_device_fields_survive_unchanged(self):
        payload = bytes([0x80, 0x81, 0x00, 0x7F, 0xFF, 0x01, 0x80])
        line = support.csi_line(payload, seq=4294967295, local_timestamp=987654321, first_word=1)
        session, _ = self.run_session([(0.0, line)])
        raw, lines, _, info = self.read_session(session)

        row = lines[0]
        field = raw[int(row["data_byte_offset"]):int(row["data_byte_offset"]) + int(row["data_byte_length"])]
        self.assertEqual(base64.b64decode(field, validate=True), payload)
        self.assertEqual(int(row["decoded_length"]), len(payload))
        self.assertEqual(row["seq"], "4294967295")
        self.assertEqual(row["local_timestamp"], "987654321")
        self.assertEqual(row["first_word"], "1")
        self.assertEqual(row["compensate_gain"], "1.000000")
        self.assertEqual(row["parse_ok"], "1")
        self.assertEqual(info["counters"]["first_word_invalid_lines"], 1)

    def test_split_across_chunks_never_reorders_or_loses_bytes(self):
        stream_bytes = b"".join(support.csi_line(bytes([i]) * (i + 1), seq=i) for i in range(5))
        events = [(0.0, stream_bytes)]
        session, _ = self.run_session(events, chunk_size=7)
        raw, lines, chunks, info = self.read_session(session)
        self.assertEqual(raw, stream_bytes)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(len(lines), 5)
        self.assertEqual(info["counters"]["csi_parse_ok"], 5)
        self.assertEqual(info["payload_length_counts"], {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1})


class MalformedInputTest(RecorderTestCase):
    def test_malformed_lines_are_indexed_not_dropped(self):
        good = support.csi_line(bytes(16), seq=1)
        bad_b64 = support.csi_line(b"", seq=2, data_text="!!!!not base64!!!!")
        mismatch = support.csi_line(b"abcd", seq=3, declared_len=234)
        truncated = b"CSI_DATA,4,-40\r\n"
        lf_only = support.csi_line(bytes(8), seq=5).replace(b"\r\n", b"\n")
        stream_bytes = good + bad_b64 + mismatch + truncated + lf_only
        session, _ = self.run_session([(0.0, stream_bytes)])
        raw, lines, _, info = self.read_session(session)

        self.assertEqual(raw, stream_bytes)
        self.assertEqual(len(lines), 5)
        self.assertEqual([r["parse_ok"] for r in lines], ["1", "0", "0", "0", "1"])
        self.assertIn("base64_error", lines[1]["error"])
        self.assertIn("len_mismatch", lines[2]["error"])
        self.assertEqual(lines[2]["len"], "234")
        self.assertIn("field_count", lines[3]["error"])
        self.assertEqual(lines[4]["terminator"], "lf")
        counters = info["counters"]
        self.assertEqual(counters["csi_lines"], 5)
        self.assertEqual(counters["csi_parse_error"], 3)
        self.assertEqual(counters["base64_error_lines"], 1)
        self.assertEqual(counters["len_mismatch_lines"], 1)
        self.assertEqual(info["payload_length_counts"], {"4": 1, "8": 1, "16": 1})

    def test_oversized_line_is_bounded_but_kept_raw_and_aligned(self):
        huge = b"CSI_DATA," + b"A" * (recorder.MAX_LINE_BYTES + 100) + b"\r\n"
        after = support.csi_line(bytes(4), seq=9)
        stream_bytes = support.LOG_LINE + huge + after
        session, _ = self.run_session([(0.0, stream_bytes)])
        raw, lines, _, info = self.read_session(session)

        self.assertEqual(raw, stream_bytes)
        self.assertEqual([r["kind"] for r in lines], ["other", "oversized", "csi"])
        oversized = lines[1]
        self.assertEqual(int(oversized["byte_offset"]), len(support.LOG_LINE))
        self.assertEqual(int(oversized["byte_length"]), len(huge) - 2)
        self.assertEqual(
            raw[int(oversized["byte_offset"]):int(oversized["byte_offset"]) + int(oversized["byte_length"])],
            huge[:-2],
        )
        self.assertEqual(info["counters"]["oversized_lines"], 1)
        self.assertEqual(lines[2]["seq"], "9")

    def test_trailing_partial_line_is_retained_and_accounted(self):
        partial = b"CSI_DATA,11,-40,-92,0,36,36,1234,119,1,4,0,1.000000,0,AAAA"
        stream_bytes = support.csi_line(bytes(4), seq=10) + partial
        session, _ = self.run_session([(0.0, stream_bytes)])
        raw, lines, _, info = self.read_session(session)

        self.assertEqual(raw, stream_bytes)
        self.assertEqual(info["counters"]["trailing_partial_bytes"], len(partial))
        tail = lines[-1]
        self.assertEqual(tail["kind"], "trailing_partial")
        self.assertEqual(tail["terminator"], "none")
        self.assertEqual(tail["error"], "no_terminator_at_end_of_capture")
        self.assertEqual(raw[int(tail["byte_offset"]):], partial)
        self.assertEqual(info["counters"]["csi_lines"], 1)

    def test_trailing_partial_row_is_timed_by_its_own_chunk_not_by_the_end(self):
        partial = b"CSI_DATA,11,-40,-92,0,36,36,1234,119,1,4,0,1.000000,0,AAAA"
        stream_bytes = support.csi_line(bytes(4), seq=10) + partial
        # Bytes arrive early, then the port stays silent until the duration ends.
        session, _ = self.run_session([(0.0, stream_bytes)], duration=40.0, idle_timeout=60.0)
        raw, lines, chunks, info = self.read_session(session)

        tail = lines[-1]
        self.assertEqual(tail["kind"], "trailing_partial")
        last_chunk = chunks[int(tail["arrival_chunk_index"])]
        self.assertEqual(last_chunk, chunks[-1])
        # Times come from the chunk the bytes actually arrived in, not from finalize.
        self.assertEqual(tail["host_elapsed_s"], last_chunk["host_elapsed_s"])
        self.assertEqual(tail["host_utc"], last_chunk["host_utc"])
        self.assertLess(float(tail["host_elapsed_s"]), 1.0)
        # The real end of the capture stays recorded separately.
        self.assertGreater(info["timing"]["elapsed_host_s"], 39.0)
        # Raw bytes and offsets are untouched by the timing fix.
        self.assertEqual(raw, stream_bytes)
        self.assertEqual(int(tail["byte_offset"]), len(stream_bytes) - len(partial))
        self.assertEqual(int(tail["byte_length"]), len(partial))
        self.assertEqual(info["counters"]["trailing_partial_bytes"], len(partial))


class TimingTest(RecorderTestCase):
    def test_gaps_are_preserved_not_compressed(self):
        first = support.csi_line(bytes(4), seq=1)
        second = support.csi_line(bytes(4), seq=2)
        session, _ = self.run_session([(0.0, first), (30.0, second)], duration=40.0, idle_timeout=60.0)
        _, lines, chunks, info = self.read_session(session)

        gap = float(chunks[1]["host_elapsed_s"]) - float(chunks[0]["host_elapsed_s"])
        self.assertGreater(gap, 29.0)
        self.assertGreater(float(lines[1]["host_elapsed_s"]) - float(lines[0]["host_elapsed_s"]), 29.0)
        self.assertGreater(info["timing"]["elapsed_host_s"], 39.0)
        self.assertLess(info["timing"]["first_byte_elapsed_s"], 1.0)
        self.assertGreater(info["timing"]["last_byte_elapsed_s"], 29.0)
        # Host receipt time stays separate from the device timestamp column.
        self.assertEqual(lines[0]["local_timestamp"], lines[1]["local_timestamp"])
        self.assertNotEqual(lines[0]["host_utc"], lines[1]["host_utc"])


class StatusTest(RecorderTestCase):
    def test_status_recording_is_written_before_capture(self):
        clock = support.FakeClock()
        session = self.make_session(clock)
        info = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], recorder.STATUS_RECORDING)
        self.assertEqual(info["conditions"]["location"], "옆 교수회의실")
        self.assertEqual(sorted(info["unknown_conditions"]), ["notes", "occupancy"])
        self.assertFalse(info["verification"]["tx_power_verified"])
        self.assertFalse(info["interpretation"]["iq_decoded"])

    def test_idle_timeout_marks_failure_and_keeps_files(self):
        session, info = self.run_session([], duration=30.0, idle_timeout=1.0)
        self.assertEqual(info["status"], recorder.STATUS_ERROR)
        self.assertEqual(info["error_type"], "idle_timeout")
        self.assertTrue((session.path / "serial.bin").exists())
        self.assertEqual((session.path / "serial.bin").read_bytes(), b"")
        self.assertEqual(info["counters"]["raw_bytes"], 0)
        self.assertEqual(info["raw_sha256"], hashlib.sha256(b"").hexdigest())

    def test_no_csi_but_logs_completes_with_zero_csi_lines(self):
        session, info = self.run_session([(0.0, support.LOG_LINE)], duration=2.0, idle_timeout=5.0)
        self.assertEqual(info["status"], recorder.STATUS_COMPLETE)
        self.assertEqual(info["counters"]["csi_lines"], 0)
        self.assertEqual(info["counters"]["other_lines"], 1)

    def test_keyboard_interrupt_preserves_partial_session(self):
        line = support.csi_line(bytes(8), seq=1)
        session, info = self.run_session(
            [(0.0, line), (0.5, KeyboardInterrupt())], duration=60.0, idle_timeout=30.0
        )
        raw, lines, _, info = self.read_session(session)
        self.assertEqual(info["status"], recorder.STATUS_INTERRUPTED)
        self.assertEqual(info["error_type"], "keyboard_interrupt")
        self.assertEqual(raw, line)
        self.assertEqual(len(lines), 1)
        self.assertEqual(info["raw_sha256"], hashlib.sha256(line).hexdigest())

    def test_disconnect_marks_error_and_keeps_received_bytes(self):
        line = support.csi_line(bytes(8), seq=1)
        session, info = self.run_session(
            [(0.0, line), (0.5, OSError("device disconnected"))], duration=60.0, idle_timeout=30.0
        )
        raw, _, _, info = self.read_session(session)
        self.assertEqual(info["status"], recorder.STATUS_ERROR)
        self.assertEqual(info["error_type"], "serial_error")
        self.assertIn("device disconnected", info["error"])
        self.assertEqual(raw, line)

    def test_open_failure_finalizes_session_as_error(self):
        clock = support.FakeClock()
        session = self.make_session(clock)
        info = session.fail_before_capture("could not open COM3: access denied", "serial_open_error")
        self.assertEqual(info["status"], recorder.STATUS_ERROR)
        self.assertIsNone(info["started_at_utc"])
        stored = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["error_type"], "serial_open_error")
        self.assertFalse((session.path / "serial.bin").exists())


class _FailingWrites:
    """File wrapper that fails like a full disk after `allow` successful writes."""

    def __init__(self, handle, allow: int) -> None:
        self._handle = handle
        self._allow = allow

    def write(self, data):
        if self._allow <= 0:
            raise OSError(28, "No space left on device")
        self._allow -= 1
        return self._handle.write(data)

    def flush(self):
        self._handle.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False


class DiskFailureTest(RecorderTestCase):
    def fail_writes_to(self, filename: str, allow: int = 0):
        """Patch Path.open so writing `filename` fails like a full disk."""
        real_open = Path.open

        def fake_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path.name == filename and "x" in mode:
                return _FailingWrites(handle, allow)
            return handle

        patcher = mock.patch.object(Path, "open", fake_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_raw_write_failure_reports_error_and_keeps_what_reached_disk(self):
        self.fail_writes_to("serial.bin")
        session, info = self.run_session([(0.0, support.csi_line(bytes(8), seq=1))], duration=2.0, idle_timeout=5.0)

        self.assertEqual(info["status"], recorder.STATUS_ERROR)
        self.assertEqual(info["error_type"], "io_error")
        self.assertIn("No space left on device", info["error"])
        # Nothing reached the raw file, and nothing pretends it did.
        self.assertEqual((session.path / "serial.bin").read_bytes(), b"")
        self.assertEqual(info["counters"]["raw_bytes"], 0)
        self.assertEqual(info["raw_sha256"], hashlib.sha256(b"").hexdigest())
        stored = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], recorder.STATUS_ERROR)

    def test_index_file_that_cannot_be_created_aborts_without_claiming_success(self):
        clock = support.FakeClock()
        session = self.make_session(clock)
        (session.path / "chunks.csv").write_bytes(b"pre-existing bytes\n")
        stream = support.FakeSerial(clock, [(0.0, support.csi_line(bytes(8), seq=1))])

        info = session.record(stream, 2.0, 5.0)
        self.assertEqual(info["status"], recorder.STATUS_ERROR)
        self.assertEqual(info["error_type"], "io_error")
        # The file that was already there is left exactly as it was.
        self.assertEqual((session.path / "chunks.csv").read_bytes(), b"pre-existing bytes\n")

    def test_failed_final_metadata_write_raises_and_leaves_session_unfinalized(self):
        line = support.csi_line(bytes(8), seq=1)
        clock = support.FakeClock()
        session = self.make_session(clock)
        stream = support.FakeSerial(clock, [(0.0, line)])
        real_write_json = recorder._write_json

        def failing_write_json(path, value):
            if value.get("status") != recorder.STATUS_RECORDING:
                raise OSError(28, "No space left on device")
            return real_write_json(path, value)

        with mock.patch.object(recorder, "_write_json", failing_write_json):
            with self.assertRaises(recorder.FinalizeError) as ctx:
                session.record(stream, 2.0, 5.0)

        self.assertIn("No space left on device", str(ctx.exception))
        # Received bytes are kept; the stored status stays honestly unfinalized.
        self.assertEqual((session.path / "serial.bin").read_bytes(), line)
        stored = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], recorder.STATUS_RECORDING)
        self.assertEqual(len(self.read_rows(session.path / "lines.csv")), 1)


class SessionIdTest(RecorderTestCase):
    def test_duplicate_session_is_refused_without_touching_existing_bytes(self):
        clock = support.FakeClock()
        original = recorder.new_session_id
        recorder.new_session_id = lambda now: "fixed_session_id"
        self.addCleanup(setattr, recorder, "new_session_id", original)

        session = self.make_session(clock)
        (session.path / "serial.bin").write_bytes(b"existing raw bytes")
        before = (session.path / "serial.bin").read_bytes()
        before_meta = (session.path / "session.json").read_bytes()

        with self.assertRaises(FileExistsError):
            self.make_session(clock)
        self.assertEqual((session.path / "serial.bin").read_bytes(), before)
        self.assertEqual((session.path / "session.json").read_bytes(), before_meta)

    def test_ids_are_unique(self):
        clock = support.FakeClock()
        ids = {recorder.new_session_id(clock.utcnow()) for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_duration_and_idle_timeout_must_be_finite_positive(self):
        clock = support.FakeClock()
        session = self.make_session(clock)
        stream = support.FakeSerial(clock, [])
        for duration, idle in ((0, 1.0), (-1, 1.0), (float("inf"), 1.0), (1.0, 0), (1.0, float("nan"))):
            with self.assertRaises(ValueError):
                session.record(stream, duration, idle)


if __name__ == "__main__":
    unittest.main()
