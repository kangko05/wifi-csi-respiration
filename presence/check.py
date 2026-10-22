"""Scores Presence ticks against the session markers.

A label marker opens a segment that runs to the next label marker (or the last tick). The
label is the marker text's first word, matched as a prefix so `거실에 있음` is a `거실`
marker. A text holding `(HH:MM` was written after the fact and starts at that clock time,
but never before the previous label marker. Ticks in the first `SETTLE_S` of a segment are
not scored.
"""

import datetime as dt
import re
from dataclasses import dataclass

from breathing.markers import Marker
from presence.judge import ROOMS, HomeState, HomeTick, RoomState

SETTLE_S = 90.0
MIN_SHARE = 0.8

OCCUPIED_ROOM = frozenset({RoomState.MOTION, RoomState.STILL_BREATHING})
EMPTY_ROOM = frozenset({RoomState.EMPTY})


@dataclass(frozen=True)
class Expectation:
    home: HomeState
    rooms: dict[int, frozenset[RoomState]]  # rooms left out are not scored


def _person_in(room: int) -> Expectation:
    return Expectation(HomeState.OCCUPIED, {r: OCCUPIED_ROOM if r == room else EMPTY_ROOM for r in ROOMS})


LABELS: dict[str, Expectation] = {
    "외출": Expectation(HomeState.EMPTY, {r: EMPTY_ROOM for r in ROOMS}),
    "귀가": Expectation(HomeState.OCCUPIED, {}),
    "기상": Expectation(HomeState.OCCUPIED, {}),
    "침실": _person_in(1),
    "취침": _person_in(1),
    "거실": _person_in(2),
    "화장실": _person_in(3),
}

BACKDATE = re.compile(r"\((\d{1,2}):(\d{2})")


@dataclass(frozen=True)
class Segment:
    label: str
    text: str
    start: float
    end: float
    expect: Expectation


@dataclass(frozen=True)
class SegmentScore:
    segment: Segment
    ticks: int
    home_share: float
    room_share: float  # every scored room in its expected state at once
    room_shares: dict[int, float]
    where_share: float | None  # `where` names the expected room; None for a label without one

    @property
    def passed(self) -> bool:
        return self.ticks > 0 and self.home_share >= MIN_SHARE and self.room_share >= MIN_SHARE


def label_of(text: str) -> str | None:
    return next((label for label in LABELS if text.startswith(label)), None)


def segments(markers: list[Marker], last_time: float, tz: dt.timezone) -> list[Segment]:
    labelled = [(m, label) for m in markers if (label := label_of(m.text)) is not None]
    starts = []

    for i, (marker, _) in enumerate(labelled):
        start = marker.time
        written = BACKDATE.search(marker.text)

        if written is not None:
            day = dt.datetime.fromtimestamp(marker.time, tz).date()
            stated = dt.datetime.combine(day, dt.time(int(written[1]), int(written[2])), tz).timestamp()
            floor = labelled[i - 1][0].time if i > 0 else stated
            start = min(marker.time, max(stated, floor))

        starts.append(start)

    ends = starts[1:] + [last_time]
    return [Segment(label, marker.text, start, end, LABELS[label])
            for (marker, label), start, end in zip(labelled, starts, ends) if end > start]


def score(segment: Segment, ticks: list[HomeTick]) -> SegmentScore:
    inside = [t for t in ticks if segment.start + SETTLE_S <= t.time < segment.end]
    n = len(inside)

    def share(hits: int) -> float:
        return hits / n if n else 0.0

    expect = segment.expect
    room_ok = [{room: t.room(room).state in states for room, states in expect.rooms.items()} for t in inside]
    expected_room = next((room for room, states in expect.rooms.items() if states == OCCUPIED_ROOM), None)
    return SegmentScore(segment=segment, ticks=n,
                        home_share=share(sum(t.home is expect.home for t in inside)),
                        room_share=share(sum(all(ok.values()) for ok in room_ok)),
                        room_shares={room: share(sum(ok[room] for ok in room_ok)) for room in expect.rooms},
                        where_share=None if expected_room is None else share(sum(t.where == expected_room for t in inside)))
