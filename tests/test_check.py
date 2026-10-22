"""Ticket 05: markers and the marker-based replay check on a synthetic session."""

import re
import shutil
import tempfile
from pathlib import Path

import pytest

from breathing.cli import main
from breathing.markers import DOLL_ON, PAUSE, RESUME, Marker, read_markers, write_marker
from fake_stream import write_fake_session

START = 1_792_400_000.0
PAUSES = ((120.0, 165.0), (195.0, 240.0))  # the doll breathes 120 s after the last resume


@pytest.fixture(scope="module")
def fake_session():
    with tempfile.TemporaryDirectory() as path:
        session = Path(path)
        write_fake_session(session, baseline_s=90.0, breathe_s=360.0, pauses=PAUSES, start=START)
        yield session


def test_markers_round_trip_in_the_recorder_format(session_dir):
    write_marker(session_dir, "멈춤 1", now_ns=1_792_405_230_957_532_300)
    line = (session_dir / "markers.txt").read_text(encoding="utf-8")

    assert re.fullmatch(r"1792405230957532300\t\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\t멈춤 1\n", line)
    [marker] = read_markers(session_dir)
    assert marker.text == "멈춤 1" and abs(marker.time - 1_792_405_230.9575) < 1e-3
    assert marker.has_label(PAUSE) and not Marker(0.0, "멈춤이 아님").has_label(PAUSE)


def test_check_passes_the_demo_script_with_45s_pauses(fake_session, capsys):
    code = main(["check", str(fake_session)])
    lines = capsys.readouterr().out.strip().splitlines()

    assert code == 0, lines
    assert [line.split()[0] for line in lines] == ["PASS"] * 5
    labels = [line.split(": ", 1)[0].split(" ", 2)[2] for line in lines]  # "PASS HH:MM:SS <label>: <detail>"
    assert sorted(labels) == sorted([DOLL_ON, PAUSE, PAUSE, RESUME, RESUME])


def test_check_fails_and_exits_1_on_a_pause_marker_without_apnea(fake_session, session_dir, capsys):
    for path in fake_session.iterdir():
        shutil.copy(path, session_dir / path.name)

    write_marker(session_dir, PAUSE + " bogus", now_ns=int((START + 30.0) * 1e9))

    code = main(["check", str(session_dir)])
    lines = capsys.readouterr().out.strip().splitlines()

    assert code == 1
    assert any(line.startswith("FAIL") and "bogus" in line for line in lines)
