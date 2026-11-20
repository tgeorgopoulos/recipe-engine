"""
Live proof that two DESKTOP replays never run at once: replay two real desktop
recipes from two threads at the same moment and watch the second wait for the
desktop lock. Performs real keyboard/mouse actions (opens the apps the recipes
open), so run it while you are not typing.

    python tests/desktop_contention.py <recipe id A> <recipe id B>
    python tests/desktop_contention.py            # picks the first two desktop recipes
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recipe_engine import engine, recipes  # noqa: E402
from recipe_engine.config import log  # noqa: E402

ids = sys.argv[1:3]
if len(ids) < 2:
    ids = [r.id for r in recipes.load_all() if r.surface == "desktop"][:2]
if len(ids) < 2:
    print("need two desktop recipes (record two desktop workflows first)")
    sys.exit(2)
ra, rb = recipes.load(ids[0]), recipes.load(ids[1])
if ra is None or rb is None:
    print("unknown recipe id")
    sys.exit(2)

t0 = time.time()
timing: dict[str, dict] = {"A": {}, "B": {}}
waited = {"B": False}


def run(name: str, r) -> None:
    try:
        with engine.acquire_input(f"replay {r.id}", block=False):
            timing[name]["start"] = time.time() - t0
            recipes.replay(r)
    except engine.InputBusy:
        waited["B"] = True
        with engine.acquire_input(f"replay {r.id}", block=True):
            timing[name]["start"] = time.time() - t0
            recipes.replay(r)
    except Exception as e:
        log("TEST", f"{name}: {e!r}")
    finally:
        timing[name]["end"] = time.time() - t0


ta = threading.Thread(target=run, args=("A", ra))
tb = threading.Thread(target=run, args=("B", rb))
ta.start(); time.sleep(0.05); tb.start()
ta.join(); tb.join()
a, b = timing["A"], timing["B"]
log("TEST", f"A ran {a.get('start', -1):.2f}s..{a.get('end', -1):.2f}s; B ran {b.get('start', -1):.2f}s..{b.get('end', -1):.2f}s; B waited: {waited['B']}")
overlap = b.get("start", 0) < a.get("end", 0)
print("SERIAL OK" if not overlap and waited["B"] else "FAIL: desktop replays overlapped or B never waited")
sys.exit(0 if not overlap and waited["B"] else 1)
