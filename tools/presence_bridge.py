"""Forward the Presence judge's home state to the voice_box server.

  presence_bridge.py <session> [--server http://127.0.0.1:8765] [--device linaro-alip]
                     [--resend-s 60] [--stale-s 60] [--poll-s 1]

Follows <session>/presence_ticks.jsonl and POSTs {"present", "greet": true} to
{server}/device/{device}/presence when the value changes and every --resend-s seconds,
because the server forgets its state on restart. A tick older than --stale-s sends nothing,
so a stopped judge leaves the server at its own default.
"""

import argparse
import functools
import json
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

TAIL_BYTES = 16384


@dataclass(frozen=True)
class Sent:
    present: bool
    at: float


def present_from_tick(tick: dict) -> bool:
    return tick["home"] == "OCCUPIED"


def due(last: Sent | None, present: bool, tick_time: float, now: float, resend_s: float, stale_s: float) -> bool:
    if now - tick_time > stale_s:
        return False

    return last is None or present != last.present or now - last.at >= resend_s


def post_presence(server: str, device_id: str, present: bool, timeout_s: float = 3.0) -> dict:
    request = urllib.request.Request(
        f"{server.rstrip('/')}/device/{device_id}/presence",
        data=json.dumps({"present": present, "greet": True}).encode(),
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=timeout_s) as reply:
        return json.load(reply)


def follow_ticks(path: Path, poll_s: float) -> Iterator[dict | None]:
    """The last tick first, then each appended one; None when a poll found nothing new."""
    pos = None

    while True:
        ticks = []

        if path.exists():
            first = pos is None

            with open(path, "rb") as f:
                size = f.seek(0, 2)
                start = max(0, size - TAIL_BYTES) if first else pos
                f.seek(start)
                data = f.read()

            complete = data[: data.rfind(b"\n") + 1]
            pos = start + len(complete)
            lines = complete.splitlines()

            for line in lines[-1:] if first else lines:
                try:
                    ticks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        yield from ticks or [None]

        if not ticks:
            time.sleep(poll_s)


def run(ticks: Iterable[dict | None], post: Callable[[bool], dict], clock: Callable[[], float],
        resend_s: float, stale_s: float, log=print) -> None:
    latest = None
    last = None

    for item in ticks:
        latest = item or latest
        now = clock()

        if latest is None:
            continue

        present = present_from_tick(latest)

        if not due(last, present, latest["time"], now, resend_s, stale_s):
            continue

        stamp = time.strftime("%H:%M:%S", time.localtime(now))

        try:
            reply = post(present)
        except Exception as error:
            log(f"{stamp} post failed: {error}")
            continue

        last = Sent(present, now)
        log(f"{stamp} present={present} greeted={reply.get('greeted')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--device", default="linaro-alip")
    parser.add_argument("--resend-s", type=float, default=60.0)
    parser.add_argument("--stale-s", type=float, default=60.0)
    parser.add_argument("--poll-s", type=float, default=1.0)
    args = parser.parse_args()

    run(follow_ticks(args.session_dir / "presence_ticks.jsonl", args.poll_s),
        functools.partial(post_presence, args.server, args.device), time.time, args.resend_s, args.stale_s,
        log=functools.partial(print, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
