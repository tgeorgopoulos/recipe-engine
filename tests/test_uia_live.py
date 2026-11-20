"""Live UI Automation against a real Notepad: describe the editor under a point,
MOVE the window, and resolve the same element by identity - the portability claim,
on this machine's display. Drives the real desktop; opt in with -m integration."""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "win32", reason="Windows only")]


@pytest.fixture
def notepad():
    from recipe_engine import display
    display.make_dpi_aware()
    from tests._notepad import close_notepad, open_notepad
    # THIS test's Notepad window (by handle, new since launch) - never a previous test's
    # window that is still closing, and never the shared Store Notepad process.
    proc, win, hwnd = open_notepad()
    try:
        yield win, hwnd
    finally:
        close_notepad(hwnd, proc)                                    # waits until it is gone, so the next test cannot see it


def test_editor_resolves_by_identity_after_the_window_moves(notepad):
    from recipe_engine import display, uia
    win, hwnd = notepad
    r = win.BoundingRectangle
    px, py = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    t = uia.describe_at(px, py)
    assert t is not None, "nothing under the centre of Notepad"
    assert t.window and t.window["class_name"] == "Notepad", t.window
    assert t.control_type in ("EditControl", "DocumentControl", "PaneControl"), t.control_type
    # move the window somewhere else entirely: pixel coordinates are now wrong, identity is not
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 500, 300, 900, 600, 0x0040)
    time.sleep(0.4)
    el = uia.resolve(t, timeout_ms=5000)
    assert el is not None, f"could not re-find {t.label!r} after the move"
    x, y = uia.element_center_logical(el, display.current_display())
    scale = display.current_display()["scale"]
    assert 500 / scale < x < (500 + 900) / scale and 300 / scale < y < (300 + 600) / scale, (x, y)


def test_focused_control_is_described_and_is_not_a_password_field(notepad):
    from recipe_engine import uia
    t = uia.describe_focused()
    assert t is not None and t.is_password is False
    assert t.window is None or t.window["class_name"] in ("Notepad",), t.window


def test_a_recorded_click_carries_a_uia_target(notepad, monkeypatch):
    """desktop.click -> _rec -> the trace step has a `uia` block, the portable half."""
    from recipe_engine import desktop, screen
    from recipe_engine.screen import ActionResult
    win, _ = notepad
    r = win.BoundingRectangle
    got = []
    monkeypatch.setattr(desktop, "_recorder_hook", got.append)
    monkeypatch.setattr(desktop, "settle_and_shoot", lambda note: ActionResult(note=note))
    screen.screenshot_jpeg()                                           # sets the shot size the mapping uses
    sw, sh = screen.shot_size()
    pw, ph = __import__("recipe_engine.display", fromlist=["x"]).physical_screen_size()
    cx, cy = (r.left + r.right) // 2 * sw / pw, (r.top + r.bottom) // 2 * sh / ph
    desktop.click(int(cx), int(cy))
    assert got and got[0]["action"] == "click"
    assert got[0].get("uia") and got[0]["uia"]["ancestors"][0]["class_name"] == "Notepad"
    assert got[0]["rel"] and got[0]["post"]["window"]
