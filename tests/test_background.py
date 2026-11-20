"""Background replay: decided once before step 1 (pattern-only recipe AND UI Automation
answers AND the setting is on), driven purely by UIA patterns IN PLACE - no SendInput,
no focus change - and never silently switched to the foreground path mid-run. Unit
tests use a fake control tree; the live test (-m integration) sets a real Notepad's
text by ValuePattern while a separate "user window" owns the foreground, and checks
that the foreground never changed and Notepad never left the user's desktop."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from recipe_engine import config, recipes, resolver, uia, vdesk
from recipe_engine.engine import Engine

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

WIN = {"control_type": "WindowControl", "name": "Untitled - Notepad", "automation_id": "", "class_name": "Notepad"}


def _step(action, pattern, value=None, kind="pattern", is_password=False):
    return {"surface": "desktop", "action": action, "value": value, "input_kind": kind, "expect": "title=Untitled - Notepad",
            "uia": {"automation_id": "15", "name": "Text editor", "control_type": "EditControl", "class_name": "Edit",
                    "ancestors": [WIN], "pattern": pattern, "is_password": is_password},
            "pixel": {"rel": [0.5, 0.5], "display": {"w": 1920, "h": 1080, "scale": 1.0}}}


def _recipe(steps, capable=True):
    return recipes.Recipe(id="bg-unit", command_normalized="bg unit", name="bg-unit", surface="desktop",
                          display={"w": 1920, "h": 1080, "scale": 1.0}, steps=steps, background_capable=capable)


class _El:
    def __init__(self, patterns=("set_value", "invoke"), password=False):
        self.patterns, self.password, self.calls = set(patterns), password, []

    IsPassword = property(lambda self: self.password)


@pytest.fixture
def fake_uia(monkeypatch):
    """resolve() returns one fake element; perform_pattern records what was driven."""
    el = _El()
    monkeypatch.setattr(uia, "resolve", lambda t, timeout_ms=None, cancel=None: el)
    monkeypatch.setattr(uia, "window_present", lambda t, timeout_ms=1500: True)
    monkeypatch.setattr(uia, "element_center_logical", lambda c, d=None: (10, 10))

    def perform(c, pattern, value=None):
        if pattern not in c.patterns:
            raise uia.PatternUnavailable(pattern)
        c.calls.append((pattern, value))
    monkeypatch.setattr(uia, "perform_pattern", perform)
    monkeypatch.setattr(recipes, "_wait_window", lambda expected, timeout_ms, cancel=None: "Untitled - Notepad")
    return el


# ------------------------------------------------------------- pattern replay
def test_background_replay_drives_patterns_and_never_touches_pyautogui(fake_uia, monkeypatch):
    import pyautogui
    for name in ("click", "write", "hotkey", "press", "scroll"):
        monkeypatch.setattr(pyautogui, name, lambda *a, **k: pytest.fail(f"pyautogui.{name} used in background mode"))
    r = _recipe([_step("click", "invoke"), _step("type", "set_value", value="hours for week 12")])
    texts, n = recipes._replay_desktop(r, None, background=True)
    assert n == 2 and fake_uia.calls == [("invoke", None), ("set_value", "hours for week 12")]


def test_a_step_without_a_pattern_fails_as_pattern_unavailable_not_foreground(fake_uia, monkeypatch):
    import pyautogui
    monkeypatch.setattr(pyautogui, "press", lambda *a, **k: pytest.fail("fell back to real input mid-run"))
    r = _recipe([_step("click", "invoke"), _step("key", "none", value="enter", kind="input")])
    with pytest.raises(recipes.ReplayFailed) as ei:
        recipes._replay_desktop(r, None, background=True)
    assert ei.value.step == 2 and "pattern_unavailable" in ei.value.reason
    assert fake_uia.calls == [("invoke", None)], "step 1 ran, step 2 stopped the run"


def test_an_element_that_lost_its_pattern_fails_the_step(fake_uia):
    fake_uia.patterns = {"invoke"}                            # the recorded set_value is gone
    r = _recipe([_step("type", "set_value", value="x")])
    with pytest.raises(recipes.ReplayFailed) as ei:
        recipes._replay_desktop(r, None, background=True)
    assert "pattern_unavailable" in ei.value.reason and "set_value" in ei.value.reason


def test_background_typing_into_a_password_element_is_refused(fake_uia):
    fake_uia.password = True
    r = _recipe([_step("type", "set_value", value="hello")])
    with pytest.raises(recipes.ReplayFailed) as ei:
        recipes._replay_desktop(r, None, background=True)
    assert "password" in ei.value.reason and fake_uia.calls == []


def test_background_mode_never_uses_pixel_coordinates(monkeypatch):
    monkeypatch.setattr(uia, "resolve", lambda t, timeout_ms=None, cancel=None: None)
    monkeypatch.setattr(uia, "window_present", lambda t, timeout_ms=1500: True)
    now = {"w": 1920, "h": 1080, "scale": 1.0}                # identical display: pixels WOULD be allowed in the foreground
    st = _step("click", "invoke")
    assert resolver.resolve_target(st, now, 10).kind == "pixel"
    with pytest.raises(resolver.UnresolvableElement) as ei:
        resolver.resolve_target(st, now, 10, pixel_ok=False)
    assert "background" in str(ei.value)


# ------------------------------------------------------------- mode matrix
@pytest.mark.parametrize("capable,available,setting,want", [
    (True, True, True, True),
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
])
def test_mode_is_decided_once_from_capability_pyvda_and_the_setting(monkeypatch, capable, available, setting, want):
    monkeypatch.setattr(config, "BACKGROUND_REPLAY", setting)
    eng = Engine()
    eng.background_available = available
    r = _recipe([_step("click", "invoke")], capable=capable)
    assert eng._background_mode(r) is want


def test_engine_runs_background_in_place_and_reports_it(monkeypatch):
    """Pattern replay happens on the user's desktop: no scratch virtual desktop is created
    and no window is moved (a parked window loses its UIA subtree - see vdesk.py)."""
    events = []

    class NoScratch:
        def __enter__(self):
            pytest.fail("a scratch virtual desktop was created")
    monkeypatch.setattr(vdesk, "Scratch", NoScratch)
    monkeypatch.setattr(vdesk, "windows_for", lambda steps: pytest.fail("windows were looked up for moving"))
    monkeypatch.setattr(recipes, "replay", lambda r, c, steps=None, background=False: events.append(("replay", background)) or
                        recipes.ReplayResult(summary="Done.", steps_done=1, duration_s=0.1, read_texts=[]))
    from recipe_engine import abort
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: [])
    r = _recipe([_step("click", "invoke")])
    recipes.save(r)
    eng = Engine()
    eng.background_available = True
    try:
        out = eng.replay("bg-unit")
        assert out["status"] == "ok" and out["background_mode"] is True, out
        assert events == [("replay", True)]
    finally:
        recipes.forget("bg-unit")


def test_foreground_mode_shows_the_banner_and_reports_background_false(monkeypatch):
    from recipe_engine import abort, overlay
    shown = []

    class FakeBanner:
        def show(self, text=None): shown.append("show")
        def hide(self): shown.append("hide")
    monkeypatch.setattr(overlay, "banner", lambda: FakeBanner())
    monkeypatch.setattr(abort, "release_all_keys", lambda *a, **k: [])
    monkeypatch.setattr(recipes, "replay", lambda r, c, steps=None, background=False:
                        recipes.ReplayResult(summary="Done.", steps_done=1, duration_s=0.1, read_texts=[]))
    r = _recipe([_step("click", "invoke")], capable=False)
    recipes.save(r)
    try:
        out = Engine().replay("bg-unit")
        assert out["status"] == "ok" and out["background_mode"] is False
        assert shown == ["show", "hide"]
    finally:
        recipes.forget("bg-unit")


# ------------------------------------------------------------- live
@pytest.mark.integration
@win
def test_live_background_replay_sets_notepad_text_without_touching_the_foreground():
    from recipe_engine import display
    display.make_dpi_aware()
    import ctypes
    from pathlib import Path
    import uiautomation as auto
    from tests._notepad import close_notepad, open_notepad
    u = ctypes.windll.user32
    proc, w, hwnd = open_notepad()
    # "the user's window": launched last so it owns the foreground (a background process
    # cannot SetForegroundWindow reliably; a freshly shown app can Activate itself)
    user_proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File",
                                  str(Path(__file__).parent / "fixtures" / "user_window.ps1")])
    user_win = auto.WindowControl(searchDepth=1, Name="User Window (Recipe Engine test)")
    assert user_win.Exists(15, 0.2), "the user-window fixture did not open"
    time.sleep(0.5)
    user_hwnd = user_win.NativeWindowHandle
    if u.GetForegroundWindow() != user_hwnd:
        # Windows' foreground lock (another process holds it, e.g. a game-input service window)
        # means the premise cannot be set up here; the replay itself is still exercised below.
        fg_locked = True
    else:
        fg_locked = False
    title = w.Name
    # the editor as the recorder would describe it
    r_ = w.BoundingRectangle
    t = uia.describe_at((r_.left + r_.right) // 2, (r_.top + r_.bottom) // 2)
    assert t is not None and t.pattern == "set_value", t
    step = {"surface": "desktop", "action": "type", "value": "background replay wrote this", "input_kind": "pattern",
            "expect": f"title={title}", "uia": t.to_dict(), "timeout_ms": 5000}
    r = recipes.Recipe(id="live-bg", command_normalized="bg live", name="live-bg", surface="desktop",
                       display=display.current_display(), steps=[step], background_capable=True)
    recipes.save(r)
    eng = Engine()
    eng.background_available = True
    try:
        fg_mid = u.GetForegroundWindow()
        out = eng.replay("live-bg")
        assert out["status"] == "ok" and out["background_mode"] is True, out
        assert u.GetForegroundWindow() != hwnd, "the background replay raised the replayed app"
        if not fg_locked:
            assert u.GetForegroundWindow() == fg_mid, "the background replay changed the foreground window"
        # Notepad shows the first line in its title ('*<text> - Notepad'); its RichEdit's
        # ValuePattern is write-only, so the title is the honest read-back.
        assert recipes._wait_window("*background replay wrote this - Notepad", 4000) is not None,             [x for x in recipes._window_titles() if "Notepad" in x]
        from pyvda import AppView, VirtualDesktop
        assert AppView(hwnd=hwnd).desktop.id == VirtualDesktop.current().id, "Notepad left the user's desktop"
    finally:
        recipes.forget("live-bg")
        try:
            u.PostMessageW(user_win.NativeWindowHandle, 0x0010, 0, 0)
            user_proc.wait(timeout=3)
        except Exception:
            user_proc.kill()
        assert close_notepad(hwnd, proc), "the test's Notepad window did not close"
