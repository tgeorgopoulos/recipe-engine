"""events.py: the in-process bus that feeds the control panel. Agent-free by design."""
from __future__ import annotations

from recipe_engine import config, events


def test_bus_replays_history_and_keeps_latest_state():
    q = events.subscribe()
    try:
        a = events.publish("state", value="IDLE")
        config.log("T4", "a log line")
        b = events.publish("state", value="REPLAYING")
        got = [q.get(timeout=1) for _ in range(3)]
        assert [g["kind"] for g in got] == ["state", "log", "state"]
        assert got[1]["tag"] == "T4" and "a log line" in got[1]["line"]
        assert events.snapshot()["state"]["value"] == "REPLAYING"
        assert [e["id"] for e in events.history(a["id"])] == [a["id"] + 1, b["id"]]
    finally:
        events.unsubscribe(q)


def test_a_stalled_subscriber_never_blocks_a_publisher():
    q = events.subscribe(maxsize=1)
    try:
        for i in range(5):
            events.publish("state", value=str(i))       # queue.Full is swallowed, publish never raises
        assert q.qsize() == 1
    finally:
        events.unsubscribe(q)
