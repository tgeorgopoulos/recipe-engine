"""
In-process event bus (V4): the state machine publishes, the web panel subscribes.

publish(kind, **data) stamps an id and a time on the event, keeps it in a ring
(a panel that connects late replays the tail), remembers the latest event of
every STATEFUL kind (so a snapshot can be built without the agent), and hands
it to every subscriber queue. Nothing here blocks: a slow subscriber drops
events, and publish() never calls config.log() (log lines are themselves
events, which would loop).
"""
from __future__ import annotations

import collections
import itertools
import queue
import threading
import time
from typing import Any

from recipe_engine import config

# One event of each of these kinds is kept as "the current value".
STATEFUL = ("state", "mode", "settings", "plan", "command", "wake_model", "voice", "attempt")

_lock = threading.Lock()
_subs: list["queue.Queue[dict]"] = []
_ring: "collections.deque[dict]" = collections.deque(maxlen=config.EVENT_RING_SIZE)
_seq = itertools.count(1)
latest: dict[str, dict] = {}


def publish(kind: str, **data: Any) -> dict:
    ev = {"id": next(_seq), "t": round(time.time(), 3), "kind": kind, **data}
    with _lock:
        _ring.append(ev)
        if kind in STATEFUL:
            latest[kind] = ev
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass                      # a stalled panel loses events; the next snapshot catches it up
    return ev


def subscribe(maxsize: int = 500) -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue(maxsize=maxsize)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q: "queue.Queue[dict]") -> None:
    with _lock:
        if q in _subs:
            _subs.remove(q)


def history(since_id: int = 0, kinds: tuple[str, ...] | None = None) -> list[dict]:
    with _lock:
        return [e for e in _ring if e["id"] > since_id and (kinds is None or e["kind"] in kinds)]


def snapshot() -> dict[str, dict]:
    with _lock:
        return dict(latest)


# --- log lines are events too ------------------------------------------------
def _on_log(tag: str, msg: str, line: str) -> None:
    publish("log", tag=tag.strip(), msg=msg, line=line)


if _on_log not in config.LOG_SUBSCRIBERS:
    config.LOG_SUBSCRIBERS.append(_on_log)


if __name__ == "__main__":
    q = subscribe()
    publish("state", value="IDLE")
    config.log("TEST", "hello bus")
    got = [q.get(timeout=1), q.get(timeout=1)]
    assert got[0]["kind"] == "state" and got[1]["kind"] == "log" and got[1]["tag"] == "TEST", got
    assert snapshot()["state"]["value"] == "IDLE"
    assert [e["kind"] for e in history()] == ["state", "log"]
    unsubscribe(q)
    print("events self-test ok")
