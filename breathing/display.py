"""The terminal display of the live commands and its marker keyboard.

The screen is redrawn once per `REFRESH_S`: the state word as a banner (green BREATHING,
red APNEA, dim NO_SIGNAL when the terminal has colour), the current score against the
threshold, the input state, the Breathing waveform of the last window as a strip, and the
marker line. No breaths-per-minute number appears unless `debug` is on.
"""

import queue
import shutil
import sys
import threading
from dataclasses import dataclass

import numpy as np

from breathing.judge import State, Tick
from breathing.markers import MARKER_KEYS

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None

REFRESH_S = 1.0
WAVEFORM_ROWS = 7
WAVEFORM_MAX_COLUMNS = 120
CLEAR_SCREEN = "\x1b[H\x1b[J"

FONT = {
    "N": ("#   #", "##  #", "# # #", "#  ##", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", " ### "),
    "_": ("     ", "     ", "     ", "     ", "#####"),
    "S": (" ####", "#    ", " ### ", "    #", "#### "),
    "I": ("#####", "  #  ", "  #  ", "  #  ", "#####"),
    "G": (" ####", "#    ", "#  ##", "#   #", " ####"),
    "A": (" ### ", "#   #", "#####", "#   #", "#   #"),
    "L": ("#    ", "#    ", "#    ", "#    ", "#####"),
    "B": ("#### ", "#   #", "#### ", "#   #", "#### "),
    "R": ("#### ", "#   #", "#### ", "#  # ", "#   #"),
    "E": ("#####", "#    ", "#### ", "#    ", "#####"),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  "),
    "H": ("#   #", "#   #", "#####", "#   #", "#   #"),
    "P": ("#### ", "#   #", "#### ", "#    ", "#    "),
}


@dataclass(frozen=True)
class LiveView:
    title: str
    clock: str
    state: State
    since_change_s: float
    tick: Tick | None
    threshold: float | None
    baseline_note: str
    input_ok: bool
    input_age_s: float
    packets_per_s: float
    waveform_times: np.ndarray
    waveform: np.ndarray
    window_s: float
    input_buffer: str
    last_marker: str
    debug: bool


def banner(word: str) -> list[str]:
    return [" ".join(FONT[letter][row] for letter in word) for row in range(5)]


def waveform_strip(times: np.ndarray, values: np.ndarray, window_s: float, columns: int,
                   rows: int = WAVEFORM_ROWS) -> list[str]:
    """One character column per `window_s / columns` seconds, the latest at the right; a
    column shows the mean of its samples, scaled to the strip's largest swing."""
    grid = [[" "] * columns for _ in range(rows)]
    middle = (rows - 1) // 2

    if len(values) >= 2:
        end = times[-1]
        column = np.floor((times - (end - window_s)) / window_s * columns).astype(int)
        inside = (column >= 0) & (column < columns)
        sums = np.bincount(column[inside], weights=values[inside], minlength=columns)
        counts = np.bincount(column[inside], minlength=columns)
        filled = counts > 0
        means = np.where(filled, sums / np.maximum(counts, 1), 0.0)
        swing = np.abs(means).max() or 1.0

        for c in np.flatnonzero(filled):
            row = int(round(middle - means[c] / swing * middle))
            grid[row][c] = "*"

    for c in range(columns):
        if grid[middle][c] == " ":
            grid[middle][c] = "-"

    return ["|" + "".join(row) + "|" for row in grid]


def colours() -> dict:
    """ANSI colour per state when stdout is a colour terminal, else empty."""
    if not sys.stdout.isatty():
        return {}

    try:
        import colorama
    except ImportError:
        return {}

    colorama.just_fix_windows_console()
    return {State.BREATHING: colorama.Fore.GREEN, State.APNEA: colorama.Fore.RED,
            State.NO_SIGNAL: colorama.Style.DIM, "reset": colorama.Style.RESET_ALL}


def render(view: LiveView, colour: dict) -> str:
    tick = view.tick
    paint = colour.get(view.state, "")
    reset = colour.get("reset", "") if paint else ""
    columns = min(shutil.get_terminal_size((80, 24)).columns - 2, WAVEFORM_MAX_COLUMNS)
    lines = [f"{view.title}    {view.clock}", ""]
    lines += [paint + row + reset for row in banner(view.state.value)]
    lines.append("")

    reason = tick.reason if tick is not None else "waiting for the first window"
    lines.append(f"state {view.state.value} for {_hms(view.since_change_s)}    reason: {reason}")

    score = "-" if tick is None or tick.score is None else f"{tick.score:.2f}"
    threshold = "-" if view.threshold is None else f"{view.threshold:.2f}"
    lines.append(f"score {score} / threshold {threshold}    ({view.baseline_note})")

    if view.input_ok:
        lines.append(f"input ok    {view.packets_per_s:.0f} pkt/s")
    else:
        age = "never" if np.isinf(view.input_age_s) else f"{view.input_age_s:.0f} s ago"
        lines.append(f"NO INPUT    last record {age}")

    lines.append("")
    lines.append(f"Breathing waveform, last {view.window_s:.0f} s")
    lines += waveform_strip(view.waveform_times, view.waveform, view.window_s, columns)
    lines.append("")
    shortcuts = "  ".join(f"{key} {label}" for key, label in MARKER_KEYS.items())
    lines.append(f"marker> {view.input_buffer}_    [{shortcuts} | text + Enter]")
    lines.append(f"last marker: {view.last_marker or '-'}")

    if view.debug and tick is not None:
        peak = "-" if tick.peak_rpm is None else f"{tick.peak_rpm:.1f}/min"
        breath = "-" if tick.last_breath_time is None else f"{tick.time - tick.last_breath_time:.0f} s before the tick"
        lines.append(f"[debug] spectral peak {peak}    last breath {breath}    line {tick.line}")

    return "\n".join(lines) + "\n"


def _hms(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Screen:
    """Redraws in place on a terminal; appends with a separator when piped."""

    def __init__(self) -> None:
        self._clear = sys.stdout.isatty()

    def show(self, text: str) -> None:
        sys.stdout.write((CLEAR_SCREEN if self._clear else "\n----\n") + text)
        sys.stdout.flush()


class KeyInput:
    """Marker input without blocking the judge. A shortcut key on an empty line is a marker
    at once; anything else is free text ended by Enter (start it with a space when its
    first letter is a shortcut key). Windows reads keys as they come; elsewhere a thread
    reads whole lines and a line that is just a shortcut key counts as that shortcut."""

    def __init__(self) -> None:
        self.buffer = ""
        self._lines: queue.Queue[str] = queue.Queue()

        if msvcrt is None:
            threading.Thread(target=self._read_lines, daemon=True).start()

    def poll(self) -> list[str]:
        return self._poll_keys() if msvcrt is not None else self._poll_lines()

    def _poll_keys(self) -> list[str]:
        texts = []

        while msvcrt.kbhit():
            key = msvcrt.getwch()

            if key == "\x03":
                raise KeyboardInterrupt

            if key in ("\x00", "\xe0"):  # function and arrow keys arrive as two codes
                msvcrt.getwch()
            elif key in ("\r", "\n"):
                if self.buffer.strip():
                    texts.append(self.buffer.strip())

                self.buffer = ""
            elif key == "\x08":
                self.buffer = self.buffer[:-1]
            elif not self.buffer and key in MARKER_KEYS:
                texts.append(MARKER_KEYS[key])
            else:
                self.buffer += key

        return texts

    def _poll_lines(self) -> list[str]:
        texts = []

        while True:
            try:
                line = self._lines.get_nowait().strip()
            except queue.Empty:
                return texts

            if line:
                texts.append(MARKER_KEYS.get(line, line))

    def _read_lines(self) -> None:
        for line in sys.stdin:
            self._lines.put(line)
