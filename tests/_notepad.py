"""Shared helpers for the integration tests that drive a real Notepad.

Store Notepad is ONE process hosting every window (including the user's own
tabs), so a test must never kill the process: it opens its own window, finds it
by handle, and closes exactly that window - answering the "Save changes?" prompt
with "Don't save" when the test modified the text."""
from __future__ import annotations

import ctypes
import subprocess
import time
from typing import Optional


def open_notepad(timeout_s: float = 6.0):
    """Launch Notepad and return (Popen, WindowControl, hwnd) for the NEW window."""
    import uiautomation as auto
    u = ctypes.windll.user32
    before = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren() if w.ClassName == "Notepad"}
    proc = subprocess.Popen(["notepad.exe"])
    deadline = time.time() + timeout_s
    win = None
    while time.time() < deadline:
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName == "Notepad" and w.NativeWindowHandle not in before:
                win = w
                break
        if win is not None:
            break
        time.sleep(0.2)
    assert win is not None, "Notepad did not open a new window"
    hwnd = win.NativeWindowHandle
    u.SetWindowPos(hwnd, 0, 100, 100, 900, 600, 0x0040)
    u.SetForegroundWindow(hwnd)
    # SetForegroundWindow from a background process is refused while another app (the
    # terminal or client that launched us) holds the foreground lock. Clicking "at the
    # editor" would then land on whatever window is on top - so insist, and give up
    # loudly rather than click blind.
    assert _force_foreground(hwnd), (
        "the test's Notepad window could not take the foreground (another window holds the "
        "foreground lock); refusing to click blind")
    # Store Notepad restores the previous session into a new window: make sure the tab
    # we type into is a fresh, untitled one, not one of the user's own files.
    if "Untitled" not in (auto.ControlFromHandle(hwnd).Name or ""):
        _press_ctrl_n(u)
        deadline = time.time() + 5
        while time.time() < deadline and "Untitled" not in (auto.ControlFromHandle(hwnd).Name or ""):
            time.sleep(0.1)
        assert "Untitled" in (auto.ControlFromHandle(hwnd).Name or ""), "Notepad did not open an Untitled tab"
        win = auto.ControlFromHandle(hwnd)
    # wait until the window has a real on-screen rectangle (a click at a 0x0 rect would land
    # in a screen corner and trip PyAutoGUI's fail-safe)
    deadline = time.time() + 5
    while time.time() < deadline:
        r = win.BoundingRectangle
        if r.right - r.left > 200 and r.bottom - r.top > 100:
            break
        time.sleep(0.1)
    time.sleep(0.4)
    return proc, win, hwnd


def _force_foreground(hwnd: int, attempts: int = 3) -> bool:
    """Make hwnd the foreground window even when another process holds the foreground
    lock. The documented escape hatch is that the calling thread must have produced the
    last input event, so a synthetic ALT press/release precedes each retry; the last
    resort attaches to the foreground thread's input queue."""
    u = ctypes.windll.user32
    for _ in range(attempts):
        if u.GetForegroundWindow() == hwnd:
            return True
        u.keybd_event(0x12, 0, 0, 0)                               # VK_MENU down
        u.keybd_event(0x12, 0, 0x0002, 0)                          # VK_MENU up
        u.ShowWindow(hwnd, 9)                                      # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        time.sleep(0.25)
    if u.GetForegroundWindow() == hwnd:
        return True
    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    if fg_tid and fg_tid != me:
        u.AttachThreadInput(me, fg_tid, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            u.AttachThreadInput(me, fg_tid, False)
        time.sleep(0.25)
    return u.GetForegroundWindow() == hwnd


def _press_ctrl_n(u) -> None:
    """Ctrl+N: a new (Untitled) tab in Store Notepad."""
    u.keybd_event(0x11, 0, 0, 0)                                   # VK_CONTROL down
    u.keybd_event(0x4E, 0, 0, 0)                                   # N down
    u.keybd_event(0x4E, 0, 0x0002, 0)
    u.keybd_event(0x11, 0, 0x0002, 0)


def close_notepad(hwnd: int, proc: Optional[subprocess.Popen] = None, timeout_s: float = 6.0) -> bool:
    """WM_CLOSE the window; if Notepad asks to save, press "Don't save". True when gone."""
    import uiautomation as auto
    u = ctypes.windll.user32
    if not u.IsWindow(hwnd):
        return True
    u.PostMessageW(hwnd, 0x0010, 0, 0)                          # WM_CLOSE
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not u.IsWindow(hwnd):
            break
        try:
            win = auto.ControlFromHandle(hwnd)
            btn = auto.ButtonControl(searchFromControl=win, Name="Don't save")
            if btn.Exists(0.3, 0.1):
                btn.GetInvokePattern().Invoke()
        except Exception:
            pass
        time.sleep(0.2)
    gone = not u.IsWindow(hwnd)
    if proc is not None:
        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()                                     # only the launcher stub, never the shared Notepad process
            except Exception:
                pass
    return gone
