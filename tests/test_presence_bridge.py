import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from presence_bridge import Sent, due, follow_ticks, post_presence, run

POLL_S = 0.05
RESEND_S = 60.0
STALE_S = 60.0


def tick(home: str, time: float = 100.0) -> dict:
    return {"time": time, "home": home, "where": None, "rooms": []}


def test_due_sends_first_value_and_changes_and_resends_after_the_interval():
    assert due(None, True, 100.0, 100.0, RESEND_S, STALE_S)
    assert not due(Sent(True, 100.0), True, 130.0, 130.0, RESEND_S, STALE_S)
    assert due(Sent(True, 100.0), True, 160.0, 160.0, RESEND_S, STALE_S)
    assert due(Sent(True, 100.0), False, 105.0, 105.0, RESEND_S, STALE_S)


def test_due_holds_when_the_tick_is_stale_even_if_the_value_changed():
    assert not due(Sent(True, 100.0), False, 100.0, 161.0, RESEND_S, STALE_S)
    assert not due(None, True, 100.0, 161.0, RESEND_S, STALE_S)


def drive(items, post, clocks):
    clock = iter(clocks)
    run(items, post, lambda: next(clock), RESEND_S, STALE_S, log=lambda line: None)


def test_run_posts_each_change_once():
    posted = []

    def post(present):
        posted.append(present)
        return {"present": present, "greeted": False}

    items = [tick("OCCUPIED"), None, tick("OCCUPIED"), tick("EMPTY")]
    drive(items, post, [100.0, 101.0, 102.0, 103.0])

    assert posted == [True, False]


def test_run_retries_after_a_failed_post_on_the_next_item():
    posted = []

    def post(present):
        posted.append(present)

        if len(posted) == 1:
            raise OSError("connection refused")

        return {"present": present, "greeted": True}

    items = [tick("OCCUPIED"), None, None]
    drive(items, post, [100.0, 101.0, 102.0])

    assert posted == [True, True], "the failed value was retried once, then held"


def test_follow_ticks_yields_existing_and_appended_complete_lines_and_heartbeats_otherwise(session_dir):
    path = session_dir / "presence_ticks.jsonl"
    ticks = follow_ticks(path, POLL_S)

    assert next(ticks) is None, "no file yet"

    first = json.dumps(tick("OCCUPIED", 1.0))
    second = json.dumps(tick("EMPTY", 2.0))
    path.write_text(first + "\n", encoding="utf-8")

    assert next(ticks)["time"] == 1.0

    with open(path, "a", encoding="utf-8") as f:
        f.write(second[:20])

    assert next(ticks) is None, "a half-written line waits for its newline"

    with open(path, "a", encoding="utf-8") as f:
        f.write(second[20:] + "\n")

    assert next(ticks)["time"] == 2.0


def test_follow_ticks_starts_from_the_last_existing_line(session_dir):
    path = session_dir / "presence_ticks.jsonl"
    path.write_text("".join(json.dumps(tick("OCCUPIED", t)) + "\n" for t in (1.0, 2.0, 3.0)), encoding="utf-8")

    assert next(follow_ticks(path, POLL_S))["time"] == 3.0


def test_post_presence_sends_the_voice_box_request_and_returns_the_reply():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"present": True, "greeted": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        reply = post_presence(f"http://127.0.0.1:{server.server_port}", "linaro-alip", True)
    finally:
        server.shutdown()
        server.server_close()

    assert seen == {"path": "/device/linaro-alip/presence", "body": {"present": True, "greet": True}}
    assert reply == {"present": True, "greeted": True}
