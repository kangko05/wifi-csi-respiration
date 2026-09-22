"""CLI contracts. No serial port is opened anywhere in this module."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import support  # noqa: E402 - sibling module, tests/ is the discovery root
from csi_collect import cli, serialport  # noqa: E402

TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-tests"


class FakePort:
    def __init__(self, device, description, hwid):
        self.device, self.description, self.hwid = device, description, hwid


class CliTestCase(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "data"

    def run_main(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def sessions(self):
        return sorted(p for p in self.root.iterdir() if p.is_dir()) if self.root.exists() else []


class ArgumentValidationTest(CliTestCase):
    def test_help_exits_zero_without_opening_anything(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertIn("--port", text)
        self.assertIn("--duration", text)
        self.assertIn("--location", text)

    def test_required_arguments(self):
        for argv in ([], ["--port", "COM3"], ["--port", "COM3", "--duration", "5"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_duration_must_be_finite_and_positive(self):
        base = ["--port", "COM3", "--location", "옆 교수회의실", "--data-root", str(self.root)]
        for bad in ("0", "-3", "inf", "nan"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                cli.main([*base, "--duration", bad])
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("finite and positive", err.getvalue())
        self.assertEqual(self.sessions(), [])

    def test_idle_timeout_and_baud_validation(self):
        base = ["--port", "COM3", "--location", "L", "--duration", "5", "--data-root", str(self.root)]
        for extra in (["--idle-timeout", "0"], ["--baud", "0"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.main([*base, *extra])
            self.assertEqual(ctx.exception.code, 2)


class ListPortsTest(CliTestCase):
    def test_list_ports_uses_enumeration_only(self):
        ports = [FakePort("COM3", "USB Serial", "USB VID:PID=1A86:7523"), FakePort("COM4", "USB Serial", "x")]
        code, out, _ = self.run_main(["--list-ports"], comports=lambda: ports)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("COM3", out)
        self.assertIn("COM4", out)
        self.assertEqual(self.sessions(), [])

    def test_missing_pyserial_gives_actionable_message(self):
        sys.modules["serial"] = None
        sys.modules.pop("serial.tools.list_ports", None)
        self.addCleanup(sys.modules.pop, "serial", None)
        code, _, err = self.run_main(["--list-ports"])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("pyserial", err)
        self.assertIn("pip install", err)


class SerialConfigurationTest(CliTestCase):
    def test_port_is_opened_quietly_without_writes_or_buffer_clearing(self):
        ops: list = []
        clock = support.FakeClock()
        created: list = []

        def factory(**kwargs):
            handle = support.RecordingSerial(ops, clock, [(0.0, support.csi_line(bytes(8), seq=1))], **kwargs)
            created.append(handle)
            return handle

        handle = serialport.open_serial("COM3", 921600, timeout=0.05, serial_factory=factory)
        self.assertEqual(ops[0], ("construct", None, 921600, 0.05))
        self.assertEqual(ops[1:], [("set_dtr", False), ("set_rts", False), ("set_port", "COM3"), ("open", "COM3")])
        self.assertNotIn("reset_input_buffer", [op[0] for op in ops])
        self.assertEqual(bytes(handle.written), b"")
        settings = serialport.describe_settings(handle)
        self.assertEqual(settings["port"], "COM3")
        self.assertFalse(settings["input_buffer_cleared"])
        self.assertIn("may still toggle", settings["note"])


class EndToEndTest(CliTestCase):
    def test_capture_run_writes_one_session_with_raw_and_metadata(self):
        ops: list = []
        clock = support.FakeClock()
        stream_bytes = support.HEADER_LINE + support.csi_line(bytes(range(16)), seq=1)

        def factory(**kwargs):
            return support.RecordingSerial(ops, clock, [(0.0, stream_bytes)], **kwargs)

        code, out, err = self.run_main(
            [
                "--port", "COM3", "--duration", "0.2", "--location", "옆 교수회의실",
                "--data-root", str(self.root), "--notes", "",
            ],
            serial_factory=factory,
        )
        self.assertEqual(code, cli.EXIT_OK, err)
        sessions = self.sessions()
        self.assertEqual(len(sessions), 1)
        path = sessions[0]
        self.assertEqual((path / "serial.bin").read_bytes(), stream_bytes)
        info = json.loads((path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], "complete")
        self.assertEqual(info["conditions"]["location"], "옆 교수회의실")
        # Empty string stays distinct from unknown (null).
        self.assertEqual(info["conditions"]["notes"], "")
        self.assertIsNone(info["conditions"]["occupancy"])
        self.assertIn("occupancy", info["unknown_conditions"])
        self.assertNotIn("notes", info["unknown_conditions"])
        self.assertIn("--location", info["host"]["command_argv"])
        self.assertEqual(info["config"]["baud"], 921600)
        self.assertIn("raw sha256", out)
        self.assertIn(("close",), ops)

    def test_metadata_write_failure_exits_nonzero_and_closes_the_port(self):
        ops: list = []
        clock = support.FakeClock()
        stream_bytes = support.csi_line(bytes(8), seq=1)
        real_write_json = cli.recorder._write_json

        def failing_write_json(path, value):
            if value.get("status") != cli.recorder.STATUS_RECORDING:
                raise OSError(28, "No space left on device")
            return real_write_json(path, value)

        def factory(**kwargs):
            return support.RecordingSerial(ops, clock, [(0.0, stream_bytes)], **kwargs)

        with mock.patch.object(cli.recorder, "_write_json", failing_write_json):
            code, out, err = self.run_main(
                [
                    "--port", "COM3", "--duration", "0.2", "--location", "옆 교수회의실",
                    "--data-root", str(self.root),
                ],
                serial_factory=factory,
            )

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("No space left on device", err)
        self.assertNotIn("status: complete", out)
        self.assertIn(("close",), ops)
        path = self.sessions()[0]
        # Received bytes survive; the stored status stays unfinalized, not "complete".
        self.assertEqual((path / "serial.bin").read_bytes(), stream_bytes)
        info = json.loads((path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], "recording")

    def test_open_failure_records_failed_session_and_exits_nonzero(self):
        def factory(**kwargs):
            raise OSError("could not open port 'COM3': Access is denied.")

        code, _, err = self.run_main(
            [
                "--port", "COM3", "--duration", "1", "--location", "옆 교수회의실",
                "--data-root", str(self.root),
            ],
            serial_factory=factory,
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("Access is denied", err)
        sessions = self.sessions()
        self.assertEqual(len(sessions), 1)
        info = json.loads((sessions[0] / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], "error")
        self.assertEqual(info["error_type"], "serial_open_error")
        self.assertFalse((sessions[0] / "serial.bin").exists())


if __name__ == "__main__":
    unittest.main()
