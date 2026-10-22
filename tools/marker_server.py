"""Phone-friendly marker page: one tap writes a marker into the recording session, and the
top of the page shows the latest Breathing tick (ticks.jsonl, bedroom Main path) and the latest
Presence tick (presence_ticks.jsonl, every room) the live judges wrote. The page is fetched once
and polls /status.json every second.

usage: python tools/marker_server.py <session_dir> [--port 8090] [--no-apnea-hold]
Reachable over Tailscale at http://<this PC's tailscale ip>:8090/. Buttons write the label as-is;
the free-text box writes whatever is typed. --no-apnea-hold shows every APNEA as APNEA, for a demo
where nobody moves in the doll's room. Nothing else is exposed.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import socket
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from breathing.markers import BASELINE_END, BASELINE_START, DOLL_ON, PAUSE, RESUME, write_marker  # noqa: E402

LABELS = [DOLL_ON, PAUSE, RESUME, BASELINE_START, BASELINE_END, "빈방 시작", "빈방 끝", "사람 입장", "사람 퇴장", "인형 끔",
          "침실 (책상)", "침실 (누움)", "취침", "기상", "거실", "화장실", "외출 시작", "귀가", "숨 참기 시작", "숨 참기 끝"]
SESSION = Path()
APNEA_HOLD = True
CHANGES_WITHIN_S = 1800.0
ROOMS = {1: "침실", 2: "거실", 3: "화장실"}
BREATHING_TICKS = {1: "ticks.jsonl", 2: "ticks_room2.jsonl", 3: "ticks_room3.jsonl"}  # one Breathing judge per Main path
BREATHING_STATE = {"BREATHING": ("호흡 있음", "#2a2"), "APNEA": ("무호흡", "#d22"), "NO_SIGNAL": ("호흡 없음", "#666")}
ROOM_STATE = {"MOTION": "움직임", "STILL_BREATHING": "정지+호흡", "EMPTY": "비어 있음", "NO_DATA": "데이터 없음"}
STALE_S = 40.0


def last_json_line(name: str) -> dict | None:
    path = SESSION / name
    if not path.exists():
        return None
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4096))
        lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None


GATE_S = 30.0


def recent_presence(now: float, within_s: float) -> list[dict]:
    path = SESSION / "presence_ticks.jsonl"
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 16384))
        lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
    ticks = []
    for line in lines[-12:]:
        try:
            tick = json.loads(line)
        except json.JSONDecodeError:
            continue
        if now - tick["time"] <= within_s:
            ticks.append(tick)
    return ticks


def moving_recently(room: int, now: float) -> bool:
    """The room's Case link showed MOTION in a presence tick of the last GATE_S seconds."""
    return any(any(r["room"] == room and r["state"] == "MOTION" for r in t["rooms"]) for t in recent_presence(now, GATE_S))



def json_tail(name: str, size: int) -> list[dict]:
    path = SESSION / name
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - size))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    ticks = []
    for line in lines[1:] if len(lines) > 1 else lines:
        try:
            ticks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return ticks


_changes_cache: tuple[float, list[str]] = (0.0, [])


def cached_changes(now: float) -> list[str]:
    global _changes_cache
    if now - _changes_cache[0] >= 5.0:
        _changes_cache = (now, changes(now))
    return _changes_cache[1]


def changes(now: float, limit: int = 14) -> list[str]:
    out = []
    previous = {}
    for t in json_tail("presence_ticks.jsonl", 400_000):
        if now - t["time"] > CHANGES_WITHIN_S:
            continue
        stamp = datetime.fromtimestamp(t["time"]).strftime("%H:%M:%S")
        where = ROOMS.get(t.get("where"), "-") if t.get("where") is not None else ("집 비움" if t["home"] == "EMPTY" else "불명")
        if previous.get("where", where) != where:
            out.append((t["time"], f'{stamp} 위치 {previous["where"]} → {where}'))
        previous["where"] = where
        for r in t["rooms"]:
            key = f'room{r["room"]}'
            state = ROOM_STATE.get(r["state"], r["state"])
            if previous.get(key, state) != state:
                out.append((t["time"], f'{stamp} {ROOMS.get(r["room"], r["room"])} 링크 {previous[key]} → {state}'))
            previous[key] = state
    for room, name in BREATHING_TICKS.items():
        last = None
        for t in json_tail(name, 60_000):
            if now - t["time"] > CHANGES_WITHIN_S:
                continue
            label = BREATHING_STATE.get(t["state"], (t["state"], ""))[0]
            if last is not None and last != label:
                stamp = datetime.fromtimestamp(t["time"]).strftime("%H:%M:%S")
                out.append((t["time"], f'{stamp} {ROOMS[room]} 호흡 {last} → {label} (점수 {t["score"] if t["score"] is None else round(t["score"], 1)})'))
            last = label
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [text for _, text in out[:limit]]


def breathing_row(room: int, b: dict, now: float) -> dict:
    state = b["state"]
    age = now - b["time"]
    stale = age > STALE_S
    input_ok = b.get("input_ok", True)
    label, colour = BREATHING_STATE.get(state, (state, "#333"))
    hold = False
    if not input_ok:
        label, colour = "입력 끊김", "#333"
    elif stale:
        label, colour = f"{label} (판정 멈춤)", "#333"
    elif state == "APNEA" and APNEA_HOLD and moving_recently(room, now):
        label, colour, hold = "무호흡 판정, 움직임 중이라 보류", "#a60", True
    elif state == "NO_SIGNAL":
        reason = b.get("reason") or ""
        progress = re.fullmatch(r"line (\d+/\d+)", reason)
        if reason.startswith("warming"):
            label = "준비 중"
        elif progress:
            label = f"확인 중 {progress[1]}"
    score, threshold = b.get("score"), b.get("threshold")
    ratio = f" (×{score / threshold:.1f})" if score is not None and threshold else ""
    rate = f'{b["peak_rpm"]:.0f}/분' if state == "BREATHING" and b.get("line") and b.get("peak_rpm") is not None else "-"
    detail = (f'호흡수 {rate} · 점수 {"-" if score is None else f"{score:.1f}"} / 문턱 '
              f'{"-" if threshold is None else f"{threshold:.1f}"}{ratio} · {age:.0f}초 전')
    return {"room": room, "name": ROOMS[room], "state": state, "label": f"{ROOMS[room]} 호흡: {label}", "colour": colour,
            "detail": detail, "age_s": age, "stale": stale, "input_ok": input_ok, "hold": hold}


def presence_row(pr: dict, now: float) -> dict:
    age = now - pr["time"]
    stale = age > STALE_S
    best = pr.get("where")
    occupied = [ROOMS.get(r["room"], str(r["room"])) for r in pr["rooms"] if r["state"] in ("MOTION", "STILL_BREATHING")]
    if best is not None:
        where = ROOMS.get(best, str(best))
    elif occupied:
        where = ", ".join(occupied)
    else:
        where = "집 비움" if pr["home"] == "EMPTY" else "어느 방인지 불명 (집에 있음)"
    rooms = " · ".join(f'{ROOMS.get(r["room"], r["room"])} {ROOM_STATE.get(r["state"], r["state"])}' for r in pr["rooms"])
    colour = "#333" if stale else ("#555" if pr["home"] == "EMPTY" else "#27c")
    return {"label": f'위치: {where}{" (판정 멈춤)" if stale else ""}', "colour": colour,
            "detail": f"{rooms} · {age:.0f}초 전", "age_s": age, "stale": stale}


def status() -> dict:
    now = time.time()
    pr = last_json_line("presence_ticks.jsonl")
    breathing = [breathing_row(room, b, now) for room, name in BREATHING_TICKS.items() if (b := last_json_line(name)) is not None]
    return {"now": now, "session": SESSION.name, "breathing": breathing,
            "presence": presence_row(pr, now) if pr is not None else None,
            "changes": cached_changes(now), "markers": recent()}


def recent(n: int = 8) -> list[str]:
    path = SESSION / "markers.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    return [f"{p[1][11:19]}  {p[2]}" for p in (l.split("\t") for l in lines) if len(p) >= 3][::-1]



PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>마커</title>
<style>body{font-family:sans-serif;margin:16px;background:#111;color:#eee}
.st{font-size:20px;padding:12px;margin:6px 0;border-radius:10px} .st small{font-size:14px;color:#ddd}
@media (min-width:900px){.st{font-size:44px} .st small{font-size:22px}}
button{width:100%;font-size:22px;padding:16px;margin:6px 0;border:0;border-radius:10px;background:#2d6cdf;color:#fff}
input{width:70%;font-size:18px;padding:12px} .go{width:28%;display:inline-block} .msg{color:#8f8;margin:8px 0}
li{font-size:15px;margin:4px 0;color:#bbb} h1{font-size:14px;color:#999;margin:0}</style></head><body>
<h1>__SESSION__</h1><div id="status"></div><div class="msg" id="msg"></div>__BUTTONS__
<form id="free"><input id="text" placeholder="자유 텍스트"><button class="go">기록</button></form>
<div class="st" style="background:#222;font-size:15px"><small>최근 30분 변화 (최신 위)</small><div id="changes"></div></div>
<ul id="markers"></ul>
<script>
const $ = id => document.getElementById(id);
const OFFLINE = "서버 응답 없음";
const msg = text => { $("msg").textContent = text; };
function row(r) {
  const d = document.createElement("div");
  d.className = "st"; d.style.background = r.colour;
  const s = document.createElement("small"); s.textContent = r.detail;
  d.append(r.label, document.createElement("br"), s);
  return d;
}
function lines(parent, items, tag) {
  parent.replaceChildren(...items.map(t => { const e = document.createElement(tag); e.textContent = t; return e; }));
}
async function poll() {
  try {
    const s = await (await fetch("/status.json")).json();
    const rows = s.breathing.map(row);
    if (!rows.length) rows.push(row({colour: "#333", label: "호흡 판정 없음", detail: ""}));
    rows.push(row(s.presence || {colour: "#333", label: "재실 판정 없음", detail: ""}));
    $("status").replaceChildren(...rows);
    lines($("changes"), s.changes.length ? s.changes : ["변화 없음"], "div");
    lines($("markers"), s.markers, "li");
    if ($("msg").textContent === OFFLINE) msg("");
  } catch (e) { msg(OFFLINE); }
}
async function mark(text) {
  try {
    const r = await fetch("/mark", {method: "POST", body: new URLSearchParams({text})});
    msg((await r.json()).message);
    poll();
    return true;
  } catch (e) { msg(OFFLINE); return false; }
}
document.querySelectorAll("button[data-text]").forEach(b => b.onclick = () => mark(b.dataset.text));
$("free").onsubmit = async e => {
  e.preventDefault();
  if (await mark($("text").value)) $("text").value = "";
};
poll();
setInterval(poll, 1000);
</script></body></html>"""


def page() -> bytes:
    buttons = "".join(f'<button type="button" data-text="{html.escape(l)}">{html.escape(l)}</button>' for l in LABELS)
    return PAGE.replace("__SESSION__", html.escape(SESSION.name)).replace("__BUTTONS__", buttons).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/status.json":
            self._send_json(status())
        else:
            self._send(page(), "text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        text = fields.get("text", [""])[0].strip()[:200]
        message = ""
        if text:
            marker = write_marker(SESSION, text)
            message = f"기록됨 {datetime.fromtimestamp(marker.time).strftime('%H:%M:%S')}  {text}"
        self._send_json({"message": message})

    def _send_json(self, value):
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Marker page and live status for a recording session.")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-apnea-hold", action="store_true")
    args = parser.parse_args()
    SESSION = args.session_dir
    APNEA_HOLD = not args.no_apnea_hold
    print(f"marker page on http://{lan_address()}:{args.port}/ -> {SESSION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
