"""The marker-based replay check (ticket 05): the demo criteria as PASS/FAIL lines.

Every `멈춤` marker must be followed by APNEA within `APNEA_DEADLINE_S`; every `재개`
marker must be followed by BREATHING; between `인형 켬` and the first `멈춤` after it,
at least `MIN_BREATHING_FRACTION` of the ticks must be BREATHING and none APNEA, counting
from the first tick that could have confirmed BREATHING (a window plus the confirmation
windows after the marker). The numbers are the spec's demo criteria.
"""

from dataclasses import dataclass

from breathing.judge import State, Tick
from breathing.markers import (
    BASELINE_END,
    BASELINE_START,
    DOLL_ON,
    PAUSE,
    RESUME,
    Marker,
)
from breathing.scoring import JudgeConfig

APNEA_DEADLINE_S = 20.0
MIN_BREATHING_FRACTION = 0.9


@dataclass(frozen=True)
class CheckResult:
    label: str
    marker_time: float
    passed: bool
    detail: str

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"


def baseline_interval(markers: list[Marker]) -> tuple[float, float] | None:
    """The first `기준선 시작` and the first `기준선 끝` after it."""
    start = next((m.time for m in markers if m.has_label(BASELINE_START)), None)

    if start is None:
        return None

    end = next(
        (m.time for m in markers if m.has_label(BASELINE_END) and m.time > start), None
    )
    return None if end is None else (start, end)


def check_ticks(
    ticks: list[Tick], markers: list[Marker], config: JudgeConfig
) -> list[CheckResult]:
    results = []
    pauses = [m for m in markers if m.has_label(PAUSE)]

    for marker in markers:
        if marker.has_label(PAUSE):
            results.append(
                _first_state_after(
                    marker, ticks, State.APNEA, deadline_s=APNEA_DEADLINE_S
                )
            )
        elif marker.has_label(RESUME):
            results.append(
                _first_state_after(marker, ticks, State.BREATHING, deadline_s=None)
            )
        elif marker.has_label(DOLL_ON):
            end = next(
                (p.time for p in pauses if p.time > marker.time),
                ticks[-1].time if ticks else marker.time,
            )
            results.append(_breathing_segment(marker, end, ticks, config))

    return results


def _first_state_after(
    marker: Marker, ticks: list[Tick], state: State, deadline_s: float | None
) -> CheckResult:
    first = next((t for t in ticks if t.time >= marker.time and t.state is state), None)

    if first is None:
        return CheckResult(
            marker.text, marker.time, False, f"no {state.value} after the marker"
        )

    delay = first.time - marker.time
    passed = deadline_s is None or delay <= deadline_s
    limit = "" if deadline_s is None else f" (limit {deadline_s:.0f} s)"
    return CheckResult(
        marker.text, marker.time, passed, f"{state.value} after {delay:.1f} s{limit}"
    )


def _breathing_segment(
    marker: Marker, end: float, ticks: list[Tick], config: JudgeConfig
) -> CheckResult:
    earliest = marker.time + config.window_s + config.confirm_windows * config.step_s
    inside = [t for t in ticks if earliest <= t.time <= end]

    if not inside:
        return CheckResult(
            marker.text,
            marker.time,
            False,
            "no tick after the confirmation delay inside the segment",
        )

    breathing = sum(t.state is State.BREATHING for t in inside) / len(inside)
    apnea = sum(t.state is State.APNEA for t in inside)
    passed = breathing >= MIN_BREATHING_FRACTION and apnea == 0
    return CheckResult(
        marker.text,
        marker.time,
        passed,
        f"BREATHING {breathing:.0%} of {len(inside)} ticks until the first {PAUSE} "
        f"(minimum {MIN_BREATHING_FRACTION:.0%}), APNEA {apnea}",
    )
