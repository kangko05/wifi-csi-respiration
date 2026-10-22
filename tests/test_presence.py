"""The Presence room and home rules on synthetic windows, and the marker scoring on synthetic ticks."""

import datetime as dt
import json
from dataclasses import replace

from breathing.markers import Marker
from breathing.scoring import QuietBaseline, WindowScore
from presence import HomeState, PresenceConfig, PresenceJudge, RoomState, RoomThresholds
from presence.check import SETTLE_S, score, segments
from presence.features import StreamWindow
from presence.judge import RoomWindow

CONFIG = PresenceConfig(empty_minutes=3.0)
MOTION_THRESHOLD = 0.004
LINE_THRESHOLD = 6.0
MAIN_MOTION_THRESHOLD = 0.009
THRESHOLDS = {room: RoomThresholds(motion=QuietBaseline((MOTION_THRESHOLD,), k=3.0, min_ratio=1.0),
                                   line=QuietBaseline((LINE_THRESHOLD,), k=3.0, min_ratio=1.0),
                                   main_motion=QuietBaseline((MAIN_MOTION_THRESHOLD,), k=3.0, min_ratio=1.0))
              for room in CONFIG.rooms}
KST = dt.timezone(dt.timedelta(hours=9))
STILL, MOVING = 0.003, 0.006
LINE, NO_LINE = 12.0, 3.0
STRONG_LINE = LINE_THRESHOLD * PresenceConfig.strong_line_ratio + 1.0


def room(case_motion: float | None = STILL, line_score: float | None = NO_LINE, peak_rpm: float = 15.0,
         recent_motion: float | None = None, main_motion: float = 0.003) -> RoomWindow:
    recent = case_motion if recent_motion is None else recent_motion
    case = None if case_motion is None else StreamWindow(0.0, case_motion, WindowScore(0.0, 1.0, 15.0, 50, 1.0), recent)
    main = None if line_score is None else StreamWindow(0.0, main_motion, WindowScore(0.0, line_score, peak_rpm, 50, 1.0))
    return RoomWindow(case=case, main=main)


def states(tick) -> dict[int, RoomState]:
    return {t.room: t.state for t in tick.rooms}


def step(windows: dict[int, RoomWindow], time: float = 0.0):
    return PresenceJudge(CONFIG, THRESHOLDS).step(time, windows)


def test_case_link_motion_marks_its_room_moving():
    assert states(step({1: room(), 2: room(MOVING), 3: room()})) == {
        1: RoomState.EMPTY, 2: RoomState.MOTION, 3: RoomState.EMPTY}


def test_main_path_line_without_any_motion_is_still_breathing():
    assert states(step({1: room(line_score=LINE), 2: room(), 3: room()}))[1] is RoomState.STILL_BREATHING


def test_line_leaked_into_a_room_is_dropped_while_another_room_moves():
    assert states(step({1: room(line_score=LINE), 2: room(MOVING, line_score=LINE), 3: room()})) == {
        1: RoomState.EMPTY, 2: RoomState.MOTION, 3: RoomState.EMPTY}


def test_score_peak_outside_the_breathing_band_is_no_line():
    tick = step({1: room(line_score=LINE, peak_rpm=40.0), 2: room(), 3: room()})
    assert states(tick)[1] is RoomState.EMPTY
    assert tick.room(1).breathing_line is False


def test_room_without_any_window_is_no_data():
    assert states(step({1: room(None, None), 2: room(), 3: room()}))[1] is RoomState.NO_DATA


def test_home_empty_only_after_every_room_is_empty_for_the_configured_minutes():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    quiet = {r: room() for r in CONFIG.rooms}
    homes = [judge.step(t, quiet).home for t in range(0, 180, 10)]
    assert homes[:17] == [HomeState.OCCUPIED] * 17
    assert homes[17] is HomeState.EMPTY, "the 18 ticks of 0..170 s fill the 3-minute window"


def test_one_person_tick_in_the_window_is_noise_but_two_bring_the_home_back():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    quiet = {r: room() for r in CONFIG.rooms}
    for t in range(0, 180, 10):
        judge.step(t, quiet)
    assert judge.step(180.0, {**quiet, 3: room(MOVING)}).home is HomeState.EMPTY
    assert judge.step(190.0, quiet).home is HomeState.EMPTY
    assert judge.step(200.0, {**quiet, 3: room(MOVING)}).home is HomeState.OCCUPIED
    assert judge.step(210.0, quiet).home is HomeState.OCCUPIED, "both ticks are still inside the window"


def test_a_line_outside_the_breathing_band_still_counts_as_a_person():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    sleeping = {1: room(line_score=LINE, peak_rpm=5.0), 2: room(), 3: room()}
    ticks = [judge.step(t, sleeping) for t in range(0, 400, 10)]
    assert states(ticks[-1])[1] is RoomState.EMPTY, "no in-band line, so the room itself is not STILL_BREATHING"
    assert {t.home for t in ticks} == {HomeState.OCCUPIED}


def test_motion_home_rule_enters_by_case_link_only_and_stays_by_either_link():
    judge = PresenceJudge(replace(CONFIG, home_rule="motion"), THRESHOLDS)
    quiet = {r: room() for r in CONFIG.rooms}
    for t in range(0, 180, 10):
        judge.step(t, quiet)
    assert judge.step(180, quiet).home is HomeState.EMPTY
    next_door = {**quiet, 1: room(main_motion=0.02, line_score=LINE)}
    assert {judge.step(t, next_door).home for t in range(190, 300, 10)} == {HomeState.EMPTY}, \
        "Main-path motion or a line alone never brings the home back"
    judge.step(300, {**quiet, 1: room(MOVING)})
    assert judge.step(310, {**quiet, 1: room(MOVING)}).home is HomeState.OCCUPIED, "two Case-link motion ticks do"
    seated = {**quiet, 1: room(main_motion=0.02)}
    assert {judge.step(t, seated).home for t in range(320, 600, 10)} == {HomeState.OCCUPIED}, \
        "a seated person's Main-path motion keeps the home"
    doll = {**quiet, 1: room(line_score=LINE, main_motion=0.005)}
    assert [judge.step(t, doll).home for t in range(600, 800, 10)][-1] is HomeState.EMPTY, \
        "a breathing line with no motion is not a person"


def test_motion_home_rule_refuses_thresholds_without_main_motion():
    old = {room: replace(t, main_motion=None) for room, t in THRESHOLDS.items()}
    try:
        PresenceJudge(replace(CONFIG, home_rule="motion"), old)
    except ValueError as error:
        assert "main_motion" in str(error)
    else:
        raise AssertionError("a thresholds file from before main_motion must be rejected")


def test_missing_data_never_counts_toward_an_empty_home():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    windows = {1: room(None, None), 2: room(), 3: room()}
    assert {judge.step(t, windows).home for t in range(0, 600, 10)} == {HomeState.OCCUPIED}


def test_rooms_without_thresholds_are_left_out_so_a_one_room_setup_can_be_empty():
    judge = PresenceJudge(CONFIG, {1: THRESHOLDS[1]})
    ticks = [judge.step(t, {1: room()}) for t in range(0, 190, 10)]
    assert [t.room for t in ticks[0].rooms] == [1]
    assert ticks[-1].home is HomeState.EMPTY


def test_time_between_ticks_further_apart_than_a_stall_does_not_count_toward_an_empty_home():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    quiet = {r: room() for r in CONFIG.rooms}
    for t in range(0, 70, 10):
        assert judge.step(t, quiet).home is HomeState.OCCUPIED  # 60 s observed quiet
    assert judge.step(60.0 + 100.0, quiet).home is HomeState.OCCUPIED
    homes = [judge.step(t, quiet).home for t in range(170, 350, 10)]
    assert homes.index(HomeState.EMPTY) == 16, "the window is full of observed ticks again at 330"


def test_tick_is_one_json_line_with_every_room():
    line = step({1: room(), 2: room(MOVING), 3: room(line_score=LINE)}, time=100.0).to_json()
    data = json.loads(line)
    assert "\n" not in line
    assert data["home"] == "OCCUPIED"
    assert [(r["room"], r["state"]) for r in data["rooms"]] == [(1, "EMPTY"), (2, "MOTION"), (3, "EMPTY")]


def at(hms: str) -> float:
    return dt.datetime.combine(dt.date(2026, 10, 19), dt.time.fromisoformat(hms), KST).timestamp()


def test_label_markers_split_the_night_and_a_late_marker_starts_at_its_stated_time():
    markers = [Marker(at("20:28:28"), "보드 배치 완료: 침실 TX ..."),
               Marker(at("20:33:18"), "외출 시작 (담배)"),
               Marker(at("20:37:49"), "귀가"),
               Marker(at("20:40:00"), "화장실"),
               Marker(at("20:50:24"), "거실에 있음 (20:45 부터)")]
    found = [(s.label, s.start, s.end) for s in segments(markers, at("21:00:00"), KST)]
    assert found == [("외출", at("20:33:18"), at("20:37:49")), ("귀가", at("20:37:49"), at("20:40:00")),
                     ("화장실", at("20:40:00"), at("20:45:00")), ("거실", at("20:45:00"), at("21:00:00"))]


def test_segment_share_skips_the_settle_time_and_counts_every_room():
    segment = segments([Marker(at("21:00:00"), "거실")], at("21:10:00"), KST)[0]
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    ticks = []

    for t in range(int(segment.start), int(segment.end), 10):
        settling = t < segment.start + SETTLE_S
        leaked = segment.start + SETTLE_S + 100 <= t < segment.start + SETTLE_S + 200
        windows = {1: room(MOVING if settling or leaked else STILL), 2: room(MOVING), 3: room()}
        ticks.append(judge.step(float(t), windows))

    result = score(segment, ticks)
    assert result.ticks == 51  # 510 s after the settle time
    assert result.room_shares == {1: 41 / 51, 2: 1.0, 3: 1.0}
    assert result.room_share == 41 / 51
    assert result.home_share == 1.0
    assert result.passed


def test_strong_line_places_a_still_person_despite_motion_elsewhere():
    strong = CONFIG.strong_line_ratio * LINE_THRESHOLD + 1.0
    tick = step({1: room(line_score=strong), 2: room(MOVING), 3: room()})
    assert states(tick) == {1: RoomState.STILL_BREATHING, 2: RoomState.MOTION, 3: RoomState.EMPTY}
    assert tick.where == 1


def test_where_follows_the_room_whose_motion_is_recent():
    walked_out = room(MOVING * 2, recent_motion=STILL)
    walked_in = room(MOVING, recent_motion=MOVING)
    tick = step({1: room(), 2: walked_in, 3: walked_out})
    assert states(tick) == {1: RoomState.EMPTY, 2: RoomState.MOTION, 3: RoomState.MOTION}
    assert tick.where == 2


def test_where_stays_where_the_person_last_moved_until_motion_or_a_strong_line_or_an_empty_home():
    judge = PresenceJudge(replace(CONFIG, empty_minutes=1.0), THRESHOLDS)
    assert judge.step(0.0, {1: room(), 2: room(MOVING), 3: room()}).where == 2
    leak = {1: room(line_score=LINE), 2: room(), 3: room()}
    assert [judge.step(t, leak).where for t in (10.0, 20.0, 30.0)] == [2, 2, 2]
    assert judge.step(40.0, {1: room(line_score=STRONG_LINE), 2: room(), 3: room()}).where == 1
    assert judge.step(50.0, {1: room(), 2: room(), 3: room(MOVING)}).where == 3
    empty = {1: room(), 2: room(), 3: room()}
    assert [judge.step(t, empty).where for t in (60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0)][-1] is None


def test_where_is_the_room_with_the_largest_margin_and_none_when_empty():
    tick = step({1: room(MOTION_THRESHOLD * 1.05), 2: room(MOTION_THRESHOLD * 2.0), 3: room()})
    assert tick.where == 2
    assert step({1: room(), 2: room(), 3: room()}).where is None
    assert json.loads(tick.to_json())["where"] == 2


def test_weak_motion_on_every_link_does_not_move_where_but_walking_does():
    judge = PresenceJudge(CONFIG, THRESHOLDS)
    assert judge.step(0.0, {1: room(MOVING), 2: room(), 3: room()}).where == 1
    weak, weaker = MOTION_THRESHOLD * 1.15, MOTION_THRESHOLD * 1.05
    fidget = {1: room(weaker, line_score=LINE), 2: room(weak), 3: room(weak)}
    assert [judge.step(t, fidget).where for t in (10.0, 20.0, 30.0)] == [1, 1, 1]
    assert judge.step(40.0, {1: room(weaker), 2: room(), 3: room(MOVING)}).where == 3


def test_first_placement_takes_the_strongest_weak_motion_when_nothing_walks():
    weak, weaker = MOTION_THRESHOLD * 1.15, MOTION_THRESHOLD * 1.05
    assert step({1: room(weaker), 2: room(weak), 3: room()}).where == 2

