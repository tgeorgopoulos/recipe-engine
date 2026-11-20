"""A scratch virtual desktop for BACKGROUND replay, through pyvda.

Windows' public IVirtualDesktopManager cannot create or switch desktops, and
SendInput only reaches the active desktop's foreground window - so background
replay is only possible when every step is driven by a UI Automation pattern
(Invoke / SetValue / Toggle / Select: provider callbacks that need neither focus
nor the foreground) AND pyvda's undocumented COM bindings work on this Windows
build. `available()` probes that at service start; engine_status reports it.

Scratch: create a desktop, move the recipe's windows there, replay, move them
back and remove the desktop - always, even on failure.

STATUS (2026-09-20, Windows 11 26200): kept as a probe and a tool, NOT used by the
engine. A window moved off the active desktop keeps its child HWNDs but loses its
UI Automation subtree (Store Notepad: 6 nodes reachable, no DocumentControl, no
search hit), so nothing can be resolved by identity there. Background replay
therefore runs in place (recipes._replay_desktop_background): patterns only,
no input, focus restored after each step. engine_status reports both facts.
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import Any, Optional

from recipe_engine.config import log


def available() -> tuple[bool, str]:
    """Can this build create a virtual desktop? (bool, plain-English reason)."""
    if sys.platform != "win32":
        return False, "not Windows"
    try:
        import pyvda
        from pyvda import VirtualDesktop, get_virtual_desktops
        n = len(get_virtual_desktops())
        cur = VirtualDesktop.current().number
        return True, f"pyvda {getattr(pyvda, '__version__', '?')} sees {n} desktop(s), current #{cur}"
    except Exception as e:
        build = ""
        try:
            build = f" (Windows build {sys.getwindowsversion().build})"
        except Exception:
            pass
        return False, f"pyvda cannot drive virtual desktops on this build{build}: {type(e).__name__}: {str(e)[:120]}"


def windows_for(steps: list[dict[str, Any]]) -> list[int]:
    """Top-level window handles the recipe's steps target (by the recorded window
    class + title tail), deduplicated, in step order."""
    from recipe_engine import uia
    seen: list[int] = []
    for st in steps:
        u = st.get("uia") or {}
        anc = u.get("ancestors") or []
        win = anc[0] if anc else ({"class_name": u.get("class_name"), "name": u.get("name")} if u.get("control_type") == "WindowControl" else None)
        if not win:
            continue
        try:
            t = uia.UiaTarget(control_type="WindowControl", class_name=win.get("class_name") or "", name=win.get("name") or "")
            w = uia._find_window(t, time.time() + 2.0)
            if w is None:
                continue
            h = w.NativeWindowHandle
            h = int(h() if callable(h) else h or 0)
            if h and h not in seen:
                seen.append(h)
        except Exception as e:
            log("VDESK", f"window lookup failed for {win}: {e!r}")
    return seen


class Scratch:
    """`with Scratch() as sd: sd.move(hwnd); ...` - windows go back where they were on exit."""

    def __init__(self) -> None:
        self.desktop = None
        self.origin = None
        self.moved: list[tuple[int, Any]] = []          # (hwnd, original desktop)
        self.name = ""
        self.fg_before = 0

    def __enter__(self) -> "Scratch":
        from pyvda import VirtualDesktop
        self.fg_before = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        self.origin = VirtualDesktop.current()
        self.desktop = VirtualDesktop.create()
        try:
            self.desktop.rename("Recipe Engine (background replay)")
        except Exception:
            pass
        self.name = f"#{self.desktop.number}"
        log("VDESK", f"scratch desktop {self.name} created (user stays on #{self.origin.number})")
        return self

    def move(self, hwnd: int) -> bool:
        from pyvda import AppView
        try:
            view = AppView(hwnd=hwnd)
            origin = view.desktop
            view.move(self.desktop)
            self.moved.append((hwnd, origin))
            return True
        except Exception as e:
            log("VDESK", f"could not move window {hwnd:#x} to the scratch desktop: {e!r}")
            return False

    def __exit__(self, *exc) -> None:
        from pyvda import AppView
        for hwnd, origin in reversed(self.moved):
            try:
                if ctypes.windll.user32.IsWindow(hwnd):
                    AppView(hwnd=hwnd).move(origin)
            except Exception as e:
                log("VDESK", f"could not move window {hwnd:#x} back: {e!r}")
        self.moved.clear()
        try:
            if self.desktop is not None:
                self.desktop.remove(fallback=self.origin)
                log("VDESK", f"scratch desktop {self.name} removed")
        except Exception as e:
            log("VDESK", f"could not remove scratch desktop {self.name}: {e!r}")
        self.desktop = None
        restore_foreground(self.fg_before)


def restore_foreground(hwnd: int) -> bool:
    """Put the user's window back in front if something else took it. Some UIA
    providers move keyboard focus to their own control on SetValue/Invoke even
    while their window sits on the scratch desktop (Store Notepad's RichEdit does);
    the background replay calls this after every step so focus never lingers
    there. True when the foreground is `hwnd` afterwards."""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        if not hwnd or not u.IsWindow(hwnd):
            return False
        if u.GetForegroundWindow() == hwnd:
            return True
        cur = u.GetForegroundWindow()
        ct, mt = u.GetWindowThreadProcessId(cur, None), k.GetCurrentThreadId()
        u.AttachThreadInput(ct, mt, True)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            u.AttachThreadInput(ct, mt, False)
        for _ in range(10):
            if u.GetForegroundWindow() == hwnd:
                log("VDESK", f"foreground restored to {hwnd:#x}")
                return True
            time.sleep(0.03)
        log("VDESK", f"could not restore the foreground to {hwnd:#x}")
        return False
    except Exception as e:
        log("VDESK", f"could not restore the foreground: {e!r}")
        return False
