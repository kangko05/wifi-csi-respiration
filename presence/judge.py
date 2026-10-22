"""The Presence judge: per step, one state per room and one state for the home.

Room rule, applied to the windows that ended on the step:
- MOTION when the room's Case link variability is above its threshold. The Case link stays
  inside the room, so motion there belongs to that room.
- STILL_BREATHING when there is no Case link motion but the room's Main path shows a
  Breathing line and either no other room has Case link motion or the line is strong
  (`strong_line_ratio` times the line threshold). A Main path also sees the next room, but
  only faintly: on the 10-19 night a person in the living room put at most 17 on the bedroom
  path while a person lying in the bedroom put 25 to 165.
- EMPTY otherwise, NO_DATA when neither stream of the room has a window.

Home rule: EMPTY once the ticks of the last `empty_minutes` hold at most `person_tick_max` ticks with
person evidence, a person tick being MOTION or NO_DATA in any room or a Main-path score over its
line threshold in any room, whatever the peak frequency; OCCUPIED otherwise.
`where` is the one room with the strongest evidence (motion or line over its threshold), for
a display that names a single room; with one person at home the other occupied rooms are
noise from a next-door link or a drifting board.
Thresholds are median + k * MAD of each feature over a labelled empty-home interval.
"""

from collections import deque
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from breathing.scoring import JudgeConfig, QuietBaseline
from presence.features import StreamWindow


class RoomState(Enum):
    EMPTY = "EMPTY"
    MOTION = "MOTION"
    STILL_BREATHING = "STILL_BREATHING"
    NO_DATA = "NO_DATA"


class HomeState(Enum):
    OCCUPIED = "OCCUPIED"
    EMPTY = "EMPTY"


ROOMS = {1: "침실", 2: "거실", 3: "화장실"}


@dataclass(frozen=True)
class PresenceConfig:
    """Every number the Presence judge uses. Window, step, band and line floor come from the Breathing module."""

    breathing: JudgeConfig = field(default_factory=JudgeConfig)
    rooms: tuple[int, ...] = tuple(ROOMS)
    case_node: int = 1
    main_node: int = 2
    # At 10 Hz the Case link sits on its packet noise floor (std/mean 0.009) with or without a person.
    motion_rate_hz: float = 1.0
    k: float = 3.0  # threshold = median + k * MAD of the empty-home windows, for motion and line score
    # Motion threshold floor as a multiple of the median: a person next door lifts a Case link
    # median about 3 % (10-19 night), more than k * MAD of a few empty windows.
    motion_min_ratio: float = 1.1
    empty_minutes: float = 1.0
    person_tick_max: int = 1
    home_rule: str = "line"
    main_motion_min_ratio: float = 2.5
    # A line this far over its threshold places the person in that room. 10-19 night: a person
    # lying in bed scores 20-70x, the doll 40-70x; a line leaked from a seated person next door
    # reached 10.6x (21:01:20) and moved the label to the empty bedroom for 20 minutes.
    strong_line_ratio: float = 15.0
    # `where` ranks a MOTION room by its Case-link variability over only the last `where_window_s`.
    # 10-19 night: ranked by the full 60 s window, the label stayed on the room just left for 68 s.
    where_window_s: float = 20.0
    # A MOTION room takes `where` only when its recent motion is this far over its threshold.
    # 10-19 night 02:18: a person fidgeting in bed put MOTION of 1.0-1.2x on all three Case links
    # and the label hopped rooms every tick; walking shows 1.4x (bedroom) to 5.7x (bathroom).
    move_motion_ratio: float = 1.2


@dataclass(frozen=True)
class RoomWindow:
    case: StreamWindow | None
    main: StreamWindow | None


@dataclass(frozen=True)
class RoomThresholds:
    motion: QuietBaseline  # Case link variability
    line: QuietBaseline  # Main path line score
    main_motion: QuietBaseline | None = None

    @classmethod
    def from_windows(cls, windows: Iterable[RoomWindow], config: PresenceConfig) -> "RoomThresholds":
        windows = list(windows)
        motion = tuple(w.case.motion for w in windows if w.case is not None)
        line = tuple(w.main.score.score for w in windows if w.main is not None)
        main_motion = tuple(w.main.motion for w in windows if w.main is not None)

        if not motion or not line:
            raise ValueError(f"empty interval holds {len(motion)} case-link and {len(line)} main-path windows")

        return cls(motion=QuietBaseline(motion, config.k, config.motion_min_ratio),
                   line=QuietBaseline(line, config.k, config.breathing.baseline_min_ratio),
                   main_motion=QuietBaseline(main_motion, config.k, config.main_motion_min_ratio))


@dataclass(frozen=True)
class RoomTick:
    time: float
    room: int
    motion: float | None
    recent_motion: float | None
    main_motion: float | None
    line_score: float | None
    breathing_line: bool
    state: RoomState
    evidence: float = 0.0  # motion or line score over its threshold, whichever set the state; 0 when EMPTY

    def to_dict(self) -> dict:
        return {"room": self.room, "state": self.state.value, "motion": _round(self.motion, 5),
                "recent_motion": _round(self.recent_motion, 5),
                "main_motion": _round(self.main_motion, 5), "line_score": _round(self.line_score, 3),
                "breathing_line": self.breathing_line, "evidence": round(self.evidence, 2)}


@dataclass(frozen=True)
class HomeTick:
    time: float
    home: HomeState
    rooms: tuple[RoomTick, ...]
    where: int | None  # the one room the person is in; None when nobody is home

    def room(self, room: int) -> RoomTick:
        return next(t for t in self.rooms if t.room == room)

    def to_json(self) -> str:
        return json.dumps({"time": round(self.time, 3), "home": self.home.value, "where": self.where,
                           "rooms": [t.to_dict() for t in self.rooms]}, ensure_ascii=False)


class PresenceJudge:
    def __init__(self, config: PresenceConfig, thresholds: Mapping[int, RoomThresholds]) -> None:
        self._config = config
        self._thresholds = thresholds
        self._recent: deque[tuple[float, bool]] = deque()
        self._where: int | None = None
        self._home = HomeState.OCCUPIED

        if config.home_rule == "motion" and any(t.main_motion is None for t in thresholds.values()):
            raise ValueError("home rule 'motion' needs main_motion thresholds: rerun `presence thresholds`")

    def step(self, time: float, windows: Mapping[int, RoomWindow]) -> HomeTick:
        empty = RoomWindow(None, None)
        windows = {room: windows.get(room, empty) for room in self._thresholds}
        moving = {room for room, w in windows.items() if w.case is not None
                  and w.case.motion > self._thresholds[room].motion.threshold}
        rooms = tuple(self._room_tick(time, room, w, moving) for room, w in windows.items())

        person = any(self._person(t) for t in rooms)
        window_s = self._config.empty_minutes * 60.0
        self._recent.append((time, person))
        while self._recent[0][0] <= time - window_s:
            self._recent.popleft()
        covered = len(self._recent) >= round(window_s / self._config.breathing.step_s)
        away = covered and sum(p for _, p in self._recent) <= self._config.person_tick_max
        self._where = None if away else self._place(rooms)
        self._home = HomeState.EMPTY if away else HomeState.OCCUPIED
        return HomeTick(time, self._home, rooms, self._where)

    def _person(self, tick: RoomTick) -> bool:
        if tick.state in (RoomState.MOTION, RoomState.NO_DATA):
            return True
        thresholds = self._thresholds[tick.room]
        if self._config.home_rule == "line":
            return tick.line_score is not None and tick.line_score > thresholds.line.threshold
        return (self._home is not HomeState.EMPTY and tick.main_motion is not None
                and tick.main_motion > thresholds.main_motion.threshold)

    def _place(self, rooms: tuple[RoomTick, ...]) -> int | None:
        """A person changes rooms only by walking, so the label follows a strong line, then motion well
        over its threshold, and otherwise stays. 10-19 night 01:26: a person sitting still in the living
        room put lines of 8-20 on the bedroom and bathroom paths and 2-6 on the living path, so a leak
        must not move it; 02:18: a person in bed put weak MOTION on every link, so that must not either."""
        moving = [t for t in rooms if t.state is RoomState.MOTION]
        walking = [t for t in moving if t.evidence >= self._config.move_motion_ratio]
        strong = [t for t in rooms if t.state is RoomState.STILL_BREATHING
                  and t.evidence >= self._config.strong_line_ratio]

        if strong:
            return max(strong, key=lambda t: t.evidence).room
        if walking:
            return max(walking, key=lambda t: t.evidence).room
        if self._where is not None:
            return self._where
        if moving:
            return max(moving, key=lambda t: t.evidence).room

        still = [t for t in rooms if t.state is RoomState.STILL_BREATHING]
        return max(still, key=lambda t: t.evidence).room if still else None

    def _room_tick(self, time: float, room: int, window: RoomWindow, moving: set[int]) -> RoomTick:
        low, high = self._config.breathing.breathing_band_rpm
        thresholds = self._thresholds[room]
        main = window.main.score if window.main is not None else None
        line = main is not None and main.score > thresholds.line.threshold and low <= main.peak_rpm <= high
        strong = line and main.score > self._config.strong_line_ratio * thresholds.line.threshold
        evidence = 0.0

        if window.case is None and window.main is None:
            state = RoomState.NO_DATA
        elif room in moving:
            state = RoomState.MOTION
            evidence = window.case.recent_motion / thresholds.motion.threshold
        elif line and (strong or not moving - {room}):
            state = RoomState.STILL_BREATHING
            evidence = main.score / thresholds.line.threshold
        else:
            state = RoomState.EMPTY

        return RoomTick(time=time, room=room, motion=None if window.case is None else window.case.motion,
                        recent_motion=None if window.case is None else window.case.recent_motion,
                        main_motion=None if window.main is None else window.main.motion,
                        line_score=None if main is None else main.score, breathing_line=line, state=state,
                        evidence=evidence)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
