"""`preprocessing.to_CSIdata` contracts: field mapping, signed I/Q order, rejection.

The expected wire contract follows the RX firmware line
(`csi-rx/main/csi-rx.c:241-245`) and the collector's syntactic checks in
`csi_collect.wire.parse_line`: 15 comma-separated fields, integer metadata,
finite `compensate_gain`, strict base64 and decoded length equal to `len`.
Each I/Q pair is stored imag-first: byte[2k] is imag, byte[2k+1] is real,
both signed int8. Invalid lines must raise, never decode to zeros.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from preprocessing import CSIdata, to_CSIdata  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

# (wire column in lines.csv, CSIdata attribute) in firmware field order.
FIELD_MAP = (
    ("seq", "seq"),
    ("rssi", "rssi"),
    ("noise_floor", "noise_floor"),
    ("fft_gain", "fft_gain"),
    ("agc_gain", "agc_gain"),
    ("channel", "channel"),
    ("local_timestamp", "timestamp"),
    ("sig_len", "sig_len"),
    ("rx_format", "bb_format"),
    ("len", "length"),
    ("first_word", "first_word_invalid"),
    ("compensate_gain", "compensate_gain"),
    ("dropped", "dropped"),
)
INT_COLUMNS = tuple(col for col, _ in FIELD_MAP if col != "compensate_gain")

# Malformed first CSI rows recorded by the collector (session, line_index, byte_length, error).
MALFORMED_ROWS = (
    ("20260916T055449_958888_e2d24064", 0, 364, "field_count:14"),
    ("20260916T061327_440770_fcb222ce", 0, 333, "field_count:14"),
    ("20260916T062632_711148_812d036c", 0, 698, "field_count:29"),
)

SAMPLE_LINE = (
    "CSI_DATA,384306,-32,-94,20,18,36,3515310213,47,2,234,0,0.266073,0,"
    "CgsLDQwNCQ8KEAkRBhEJEQYUBhMHFQcUBRYFFQMVAhQDFgEVAhcAFQEVABYAFAMYAhYEFwEXABT/"
    "GAEYARf9GP4Y/xb/G/4bABoBGgAaAhoCGwAbAB0BGwMcAhwDHQMdBBwCHwYkBx8EIgMkBSUEJQQj"
    "AAAAAAAAJv0o+yb9Jf0k/iP+Jv0k/yX8Iv4j/SP8JP4h/ST8Ifsi/CD8IwAg/iD/IAEfACAAHQEf"
    "BBwCHv0dAhz/GwEcBRsGGgcYAxsIHAcZBhkHFgkXBBcGGAYYBRYIFgkWCRYIFgkVCRMLEgsSDBEM"
    "Eg4OCwwN"
)

MAX_REPORTED_FAILURES = 5


def build_line(payload: bytes = bytes(range(8)), **overrides) -> str:
    """Firmware-shaped CSI line without terminator; overrides replace wire columns."""
    values = {
        "seq": "7",
        "rssi": "-41",
        "noise_floor": "-92",
        "fft_gain": "20",
        "agc_gain": "18",
        "channel": "36",
        "local_timestamp": "4294967295",
        "sig_len": "47",
        "rx_format": "2",
        "len": str(len(payload)),
        "first_word": "1",
        "compensate_gain": "0.266073",
        "dropped": "3",
        "data": base64.b64encode(payload).decode("ascii"),
    }
    values.update(overrides)
    return ",".join(["CSI_DATA", *(values[col] for col, _ in FIELD_MAP), values["data"]])


def expected_iq(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    """(real, imag) as float64 from signed int8 bytes: real=odd, imag=even."""
    signed = np.frombuffer(raw, dtype=np.int8).astype(np.float64)
    return signed[1::2], signed[0::2]


def session_dirs() -> list[Path]:
    return sorted(p for p in DATA_ROOT.iterdir() if (p / "lines.csv").is_file()) if DATA_ROOT.is_dir() else []


def file_fingerprint(path: Path) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    st = path.stat()
    return st.st_size, st.st_mtime_ns, digest.hexdigest()


def read_lines_csv(session: Path) -> list[dict[str, str]]:
    with (session / "lines.csv").open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class ToCSIdataRealDataTest(unittest.TestCase):
    """Every recorded valid CSI row decodes consistently; sources stay untouched."""

    @classmethod
    def setUpClass(cls):
        cls.sessions = session_dirs()
        if not cls.sessions:
            raise unittest.SkipTest(f"no recorded sessions under {DATA_ROOT}")
        cls.fingerprints = cls._fingerprint_all()

    @classmethod
    def tearDownClass(cls):
        after = cls._fingerprint_all()
        changed = sorted(k for k in cls.fingerprints if cls.fingerprints[k] != after.get(k))
        if changed:
            raise AssertionError(f"source files changed during tests: {changed}")

    @classmethod
    def _fingerprint_all(cls) -> dict[str, tuple[int, int, str]]:
        return {
            f"{s.name}/{name}": file_fingerprint(s / name)
            for s in cls.sessions
            for name in ("serial.bin", "lines.csv")
        }

    def test_raw_matches_recorded_sha256(self):
        for session in self.sessions:
            with self.subTest(session=session.name):
                meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
                self.assertIn("raw_sha256", meta)
                self.assertEqual(self.fingerprints[f"{session.name}/serial.bin"][2], meta["raw_sha256"])

    def test_all_valid_csi_rows_decode_exactly(self):
        total = 0
        for session in self.sessions:
            with self.subTest(session=session.name):
                meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
                blob = (session / "serial.bin").read_bytes()
                checked, failures = 0, []
                for row in read_lines_csv(session):
                    if row["kind"] != "csi" or row["parse_ok"] != "1":
                        continue
                    checked += 1
                    problem = self._check_row(blob, row)
                    if problem:
                        failures.append(f"line {row['line_index']} @byte {row['byte_offset']}: {problem}")
                self.assertGreater(checked, 0, "session has no valid CSI rows")
                self.assertEqual(checked, meta["counters"]["csi_parse_ok"])
                self.assertEqual(
                    failures[:MAX_REPORTED_FAILURES], [],
                    f"{session.name}: {len(failures)}/{checked} rows failed",
                )
                total += checked
        self.assertGreater(total, 0)

    def _check_row(self, blob: bytes, row: dict[str, str]) -> str | None:
        off, length = int(row["byte_offset"]), int(row["byte_length"])
        content = blob[off:off + length].decode("ascii")
        data_off, data_len = int(row["data_byte_offset"]), int(row["data_byte_length"])
        data_text = blob[data_off:data_off + data_len].decode("ascii")
        if not content.endswith("," + data_text):
            return "data offsets do not locate the last field"
        try:
            result = to_CSIdata(content)
        except Exception as exc:  # report, do not abort the whole session
            return f"raised {type(exc).__name__}: {exc}"
        if not isinstance(result, CSIdata):
            return f"returned {type(result).__name__}"
        for col, attr in FIELD_MAP:
            want = float(row[col]) if col == "compensate_gain" else int(row[col])
            got = getattr(result, attr)
            if type(got) is not type(want) or got != want:
                return f"{attr}={got!r} ({type(got).__name__}), expected {col}={want!r}"
        raw = base64.b64decode(data_text, validate=True)
        declared = int(row["len"])
        if len(raw) != declared or int(row["decoded_length"]) != declared:
            return f"decoded {len(raw)} bytes / recorded {row['decoded_length']}, declared {declared}"
        data = result.data
        if not isinstance(data, np.ndarray) or data.dtype != np.complex128:
            return f"data type {type(data).__name__}/{getattr(data, 'dtype', None)}, expected complex128 ndarray"
        if data.shape != (declared // 2,):
            return f"shape {data.shape}, expected {(declared // 2,)}"
        real, imag = expected_iq(raw)
        if not np.array_equal(data.real, real):
            return f"real mismatch at {np.flatnonzero(data.real != real)[:5].tolist()}"
        if not np.array_equal(data.imag, imag):
            return f"imag mismatch at {np.flatnonzero(data.imag != imag)[:5].tolist()}"
        return None

    def test_recorded_malformed_rows_are_exactly_the_known_three(self):
        found = set()
        for session in self.sessions:
            for row in read_lines_csv(session):
                if row["kind"] == "csi" and row["parse_ok"] != "1":
                    found.add((session.name, int(row["line_index"]), int(row["byte_length"]), row["error"]))
        self.assertEqual(found, set(MALFORMED_ROWS))

    def test_recorded_malformed_rows_are_rejected(self):
        names = {s.name for s in self.sessions}
        for session_name, line_index, byte_length, error in MALFORMED_ROWS:
            with self.subTest(session=session_name, line=line_index, error=error):
                self.assertIn(session_name, names)
                session = DATA_ROOT / session_name
                row = read_lines_csv(session)[line_index]
                self.assertEqual(int(row["line_index"]), line_index)
                self.assertEqual((row["kind"], row["parse_ok"], row["error"]), ("csi", "0", error))
                off = int(row["byte_offset"])
                self.assertEqual(int(row["byte_length"]), byte_length)
                with (session / "serial.bin").open("rb") as fh:
                    fh.seek(off)
                    content = fh.read(byte_length).decode("ascii")
                self.assertTrue(content.startswith("CSI_DATA,"))
                self.assertNotIn("\r", content)
                self.assertNotIn("\n", content)
                with self.assertRaises(ValueError):
                    to_CSIdata(content)


class ToCSIdataSampleLineTest(unittest.TestCase):
    def test_sample_line_metadata_and_iq(self):
        result = to_CSIdata(SAMPLE_LINE)
        self.assertEqual(
            {attr: getattr(result, attr) for _, attr in FIELD_MAP},
            {
                "seq": 384306,
                "rssi": -32,
                "noise_floor": -94,
                "fft_gain": 20,
                "agc_gain": 18,
                "channel": 36,
                "timestamp": 3515310213,
                "sig_len": 47,
                "bb_format": 2,
                "length": 234,
                "first_word_invalid": 0,
                "compensate_gain": 0.266073,
                "dropped": 0,
            },
        )
        self.assertIsInstance(result.compensate_gain, float)
        self.assertEqual(result.data.dtype, np.complex128)
        self.assertEqual(result.data.shape, (117,))
        self.assertEqual(result.data[0], complex(11, 10))    # bytes 0x0A,0x0B
        self.assertEqual(result.data[-1], complex(13, 12))   # bytes 0x0C,0x0D
        self.assertEqual(result.data[60], complex(-3, 38))   # bytes 0x26,0xFD: signed real
        self.assertEqual(result.data[57], 0j)                # recorded zero bytes stay zero


class ToCSIdataSyntheticTest(unittest.TestCase):
    def assertRejected(self, line: str, msg_regex: str | None = None):
        if msg_regex is None:
            with self.assertRaises(ValueError, msg=repr(line[:80])):
                to_CSIdata(line)
        else:
            with self.assertRaisesRegex(ValueError, msg_regex, msg=repr(line[:80])):
                to_CSIdata(line)

    def test_signed_int8_extremes_and_iq_order(self):
        payload = bytes([0x80, 0x7F, 0x7F, 0x80, 0xFF, 0x01, 0x00, 0x00])
        result = to_CSIdata(build_line(payload))
        self.assertEqual(result.data.dtype, np.complex128)
        np.testing.assert_array_equal(result.data, np.array([127 - 128j, -128 + 127j, 1 - 1j, 0j]))
        self.assertEqual(result.timestamp, 4294967295)
        self.assertEqual((result.first_word_invalid, result.dropped), (1, 3))

    def test_zero_length_payload_is_empty_complex128(self):
        result = to_CSIdata(build_line(b""))
        self.assertEqual(result.length, 0)
        self.assertEqual(result.data.dtype, np.complex128)
        self.assertEqual(result.data.shape, (0,))

    def test_wrong_label(self):
        base = build_line()
        rest = base[len("CSI_DATA"):]
        for label in ("CSI_DATAX", "csi_data", "CSI", "", " CSI_DATA", "type"):
            with self.subTest(label=label):
                self.assertRejected(label + rest, "invalid data string")

    def test_too_few_fields(self):
        parts = build_line().split(",")
        self.assertEqual(len(parts), 15)
        for count in range(1, 15):
            with self.subTest(fields=count):
                self.assertRejected(",".join(parts[:count]), "invalid data string")
        for missing in range(1, 15):
            with self.subTest(missing_index=missing):
                self.assertRejected(",".join(parts[:missing] + parts[missing + 1:]), "invalid data string")
        with self.subTest(case="empty string"):
            self.assertRejected("", "invalid data string")

    def test_extra_fields(self):
        parts = build_line().split(",")
        cases = {
            "trailing comma": ",".join(parts) + ",",
            "extra field after data": ",".join(parts) + ",0",
            "extra metadata before data": ",".join(parts[:14] + ["0"] + parts[14:]),
            "extra field after label": ",".join(parts[:1] + ["0"] + parts[1:]),
            "two lines joined": ",".join(parts) + "," + ",".join(parts),
        }
        for name, line in cases.items():
            with self.subTest(case=name):
                self.assertRejected(line)

    def test_invalid_integer_metadata(self):
        for col in INT_COLUMNS:
            for bad in ("", "abc", "1.5", "0x10", "nan", "1e3"):
                with self.subTest(column=col, value=bad):
                    self.assertRejected(build_line(**{col: bad}))

    def test_invalid_compensate_gain(self):
        for bad in ("", "abc", "1,0"):
            with self.subTest(value=bad):
                self.assertRejected(build_line(compensate_gain=bad))

    def test_nonfinite_compensate_gain(self):
        # Collector contract: nan/inf parse as floats but are parse errors.
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e999"):
            with self.subTest(value=bad):
                self.assertRejected(build_line(compensate_gain=bad))

    def test_invalid_base64(self):
        good = base64.b64encode(bytes(range(8))).decode("ascii")
        cases = {
            "non-alphabet": "!!!!" + good,
            "bad padding": good[:-1],
            "inner padding": good[:2] + "=" + good[3:],
            "inner space": good[:4] + " " + good[4:],
            "inner newline": good[:4] + "\n" + good[4:],
            "non-ascii": good[:-1] + "é",
            "urlsafe alphabet": "-_-_",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertRejected(build_line(data=data))

    def test_declared_length_mismatch(self):
        payload = bytes(range(234))
        for declared in ("232", "236", "0", "512", "-234"):
            with self.subTest(declared=declared):
                self.assertRejected(build_line(payload, len=declared))

    def test_odd_decoded_byte_count(self):
        # Must be an explicit validation error, not numpy's incidental broadcast failure.
        for size in (1, 3, 233):
            payload = bytes((i * 37) % 256 for i in range(size))
            with self.subTest(decoded_bytes=size):
                with self.assertRaises(ValueError) as ctx:
                    to_CSIdata(build_line(payload))
                self.assertNotIn("broadcast", str(ctx.exception))

    def test_line_terminators_are_rejected_or_ignored(self):
        """Complete serial lines end with CRLF; a terminator must never alter the decode."""
        payload = bytes([0x80, 0x7F, 0xFF, 0x01])
        content = build_line(payload)
        reference = to_CSIdata(content)
        for term in ("\r\n", "\n", "\r"):
            with self.subTest(terminator=repr(term)):
                try:
                    result = to_CSIdata(content + term)
                except ValueError:
                    continue
                self.assertEqual(result.length, reference.length)
                self.assertEqual(result.dropped, reference.dropped)
                np.testing.assert_array_equal(result.data, reference.data)

    def test_embedded_carriage_return_is_rejected(self):
        parts = build_line().split(",")
        for index in (0, 14):
            with self.subTest(field=index):
                broken = list(parts)
                broken[index] = broken[index][:2] + "\r" + broken[index][2:]
                self.assertRejected(",".join(broken))


if __name__ == "__main__":
    unittest.main()
