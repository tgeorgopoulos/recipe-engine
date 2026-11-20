"""Esc abort: fires within the 200 ms budget, releases every key, and stops the
replay at its next check - including inside a 5 s wait. Unit tests inject a fake
key state and a fake SendInput so nothing real happens; the live test at the end
(-m integration) presses a real Esc during a real Notepad replay."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from recipe_engine import abort, config, recipes

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


# ------------------------------------------------------------- release_all_keys
@win
def test_release_emits_keyup_for_every_modifier_every_down_key_and_the_mouse():
    sent = []
    down = {0x41, 0x0D}                                     # 'A' and Enter are down (a stuck replay)
    released = abort.release_all_keys(send=lambda batch: sent.append(batch) or len(batch),
                                      key_down=lambda vk: vk in down)
    assert len(sent) == 1
    batch = sent[0]
    keyups = [i for i in batch if i.type == abort.INPUT_KEYBOARD]
    mouse = [i for i in batch if i.type == abort.INPUT_MOUSE]
    assert {i.u.ki.wVk for i in keyups} >= set(abort.MODIFIER_VKS) | down
    assert all(i.u.ki.dwFlags & abort.KEYEVENTF_KEYUP for i in keyups), "a KEYDOWN in the release batch"
    assert {i.u.mi.dwFlags for i in mouse} == {abort.MOUSEEVENTF_LEFTUP, abort.MOUSEEVENTF_RIGHTUP, abort.MOUSEEVENTF_MIDDLEUP}
    assert "vk41" in released and "mouse-left" in released


# ------------------------------------------------------------- the watchdog
def test_watchdog_fires_within_the_abort_budget(monkeypatch):
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: [])
    state = {"esc": False}
    fired = threading.Event()
    w = abort.AbortWatch(on_abort=fired.set, key_down=lambda vk: state["esc"] and vk == abort.VK_ESCAPE, hotkey=False)
    with w:
        time.sleep(0.12)                                      # the watch is running, nothing pressed
        assert not fired.is_set()
        t0 = time.time()
        state["esc"] = True
        assert fired.wait(1.0), "Esc was never noticed"
        latency = time.time() - t0
    assert latency < config.ABORT_DEADLINE_MS / 1000, f"abort took {latency * 1000:.0f} ms"
    assert w.fired.is_set() and w.reason == "esc" and w.fired_at


def test_an_esc_already_held_when_the_replay_starts_is_not_an_abort(monkeypatch):
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: [])
    state = {"esc": True}
    fired = threading.Event()
    w = abort.AbortWatch(on_abort=fired.set, key_down=lambda vk: state["esc"], hotkey=False)
    with w:
        time.sleep(0.2)
        assert not fired.is_set(), "a held Esc from before the replay counted as an abort"
        state["esc"] = False
        time.sleep(0.1)
        state["esc"] = True
        assert fired.wait(1.0)


def test_fire_releases_keys_then_cancels_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: calls.append("release") or [])
    w = abort.AbortWatch(on_abort=lambda: calls.append("cancel"), hotkey=False, key_down=lambda vk: False)
    w.fire("panel")
    w.fire("panel again")
    assert calls == ["release", "cancel"]                     # keys first, then the cancel event; no double fire
    assert w.reason == "panel"


def test_poll_interval_is_within_the_spec():
    assert config.ESC_POLL_MS <= 50 and config.ABORT_DEADLINE_MS == 200


# ------------------------------------------------------------- cancel inside a wait
def test_wait_title_stops_at_the_next_poll_when_cancelled(monkeypatch):
    from recipe_engine import display
    monkeypatch.setattr(display, "foreground_window_title", lambda: "Something else")
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    t0 = time.time()
    with pytest.raises(recipes.ReplayCancelled):
        recipes._wait_title("Untitled - Notepad", 5000, cancel)
    assert time.time() - t0 < 0.6, "the 5 s wait ignored the cancel"


def test_typing_stops_between_chunks_when_cancelled():
    cancel = threading.Event()
    written = []

    class PG:
        def write(self, s, interval=0):
            written.append(s)
            if len(written) == 2:
                cancel.set()

        def hotkey(self, *k):
            written.append("+".join(k))
    with pytest.raises(recipes.ReplayCancelled):
        recipes._type_chunked(PG(), "x" * 100, cancel)
    assert len(written) == 2, written                         # 16-char chunks: stopped after the second


def test_engine_reports_aborted_by_user_and_stops_at_the_next_step(monkeypatch):
    from recipe_engine.engine import Engine
    cancel_seen = {}

    def fake_replay(recipe, cancel, steps=None, background=False):
        cancel_seen["ev"] = cancel
        cancel.set()                                          # what the Esc watch does
        raise recipes.ReplayCancelled()
    monkeypatch.setattr(recipes, "replay", fake_replay)
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: [])
    r = recipes.Recipe(id="ab1", command_normalized="abort me", name="abort-me", surface="browser",
                       steps=[{"surface": "browser", "action": "goto", "value": "http://127.0.0.1:8765/", "expect": "url=http://127.0.0.1:8765/"}])
    recipes.save(r)
    try:
        out = Engine().replay("abort-me")
        assert out["status"] == "failed" and out["failure_reason"] == "aborted_by_user"
        again = recipes.load("ab1")
        assert again.runs.get("fail", 0) == 0, "an abort must not count toward quarantine"
    finally:
        recipes.forget("ab1")


# ------------------------------------------------------------- live
@pytest.mark.integration
@win
def test_live_esc_during_a_notepad_replay_aborts_and_leaves_no_stuck_key(monkeypatch):
    """A real desktop recipe types into a real Notepad; a real Esc keypress lands
    mid-typing. Expected: aborted_by_user, Notepad closes without a save prompt
    hang, and no modifier is left down."""
    import ctypes
    from recipe_engine import display
    from recipe_engine.engine import Engine
    display.make_dpi_aware()
    from tests._notepad import close_notepad, open_notepad
    u = ctypes.windll.user32
    proc, win_, hwnd = open_notepad()
    r = recipes.Recipe(id="live-abort", command_normalized="type a long note", name="live-abort", surface="desktop",
                       display=display.current_display(), steps=[
                           {"surface": "desktop", "action": "type", "value": "abort me " * 60, "expect": "title=Notepad",
                            "input_kind": "input", "timeout_ms": 3000},
                       ])
    recipes.save(r)
    try:
        def press_esc_soon():
            time.sleep(0.6)
            u.keybd_event(0x1B, 0, 0, 0)
            time.sleep(0.05)
            u.keybd_event(0x1B, 0, 2, 0)
        threading.Thread(target=press_esc_soon, daemon=True).start()
        t0 = time.time()
        out = Engine().replay("live-abort")
        took = time.time() - t0
        assert out["status"] == "failed" and out["failure_reason"] == "aborted_by_user", out
        assert took < 4.0, f"the abort did not stop the typing promptly ({took:.1f}s)"
        for vk in abort.MODIFIER_VKS:
            assert not (u.GetAsyncKeyState(vk) & 0x8000), f"modifier {vk:#x} left down"
    finally:
        recipes.forget("live-abort")
        assert close_notepad(hwnd, proc), "the test's Notepad window did not close"
