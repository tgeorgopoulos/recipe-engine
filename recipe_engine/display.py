"""Screen geometry and the desktop-control permission check. Windows-first.

Everything here is physical-vs-logical pixel bookkeeping: pyautogui and UI Automation
speak different units on a scaled display, and a recipe recorded at one DPI must
never click blind at another. `display_matches()` is the lock that enforces that
for pixel-fallback steps.

Lifted from the original executor.py / recipes.py without behavioural change.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any, Optional

from recipe_engine import config

_dpi_aware_done = False


def make_dpi_aware() -> None:
    """Windows: opt the process into per-monitor DPI awareness so pyautogui /
    ImageGrab operate in physical pixels. No-op elsewhere. Call this BEFORE importing
    uiautomation (it sets awareness at import and the first caller wins)."""
    global _dpi_aware_done
    if _dpi_aware_done or sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    _dpi_aware_done = True


def physical_screen_size() -> tuple[int, int]:
    """Actual pixel dimensions of the primary display (not logical/scaled)."""
    make_dpi_aware()
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    if sys.platform == "darwin":
        from PIL import ImageGrab
        img = ImageGrab.grab()
        return img.size
    import pyautogui
    return tuple(pyautogui.size())


def dpi_scale_factor() -> float:
    """physical / logical. 1.0 on a 100% display; 2.0 on Retina; 1.25/1.5 on scaled Windows."""
    import pyautogui
    pw, _ = physical_screen_size()
    lw, _ = pyautogui.size()
    return pw / lw if lw else 1.0


def foreground_window_title() -> str:
    """Title of the foreground window (desktop recipes assert on it after each step). "" if unknown."""
    if sys.platform != "win32":
        return ""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Display stamp: the lock on pixel-fallback steps
# ---------------------------------------------------------------------------
def current_display() -> dict[str, Any]:
    w, h = physical_screen_size()
    return {"w": int(w), "h": int(h), "scale": round(float(dpi_scale_factor()), 2)}


def display_matches(stamp: Optional[dict], now: Optional[dict] = None) -> bool:
    """Exact match on (w, h, scale). A missing stamp never matches: a pixel step with
    no recorded display is not replayable by pixels."""
    if not stamp:
        return False
    now = now or current_display()
    return (int(stamp.get("w", -1)), int(stamp.get("h", -1)), round(float(stamp.get("scale", -1)), 2)) == \
           (int(now["w"]), int(now["h"]), round(float(now["scale"]), 2))


def rel_to_logical(rel: tuple[float, float] | list[float], now: Optional[dict] = None) -> tuple[int, int]:
    """Fractional screen coords -> pyautogui (logical) coords for the CURRENT display."""
    now = now or current_display()
    scale = float(now.get("scale") or 1.0)
    return int(round(float(rel[0]) * int(now["w"]) / scale)), int(round(float(rel[1]) * int(now["h"]) / scale))


# ---------------------------------------------------------------------------
# Startup permission check (fail loudly)
# ---------------------------------------------------------------------------
@dataclass
class PermissionCheck:
    ok: bool
    message: str
    physical: tuple[int, int]
    scale: float
    elevated: bool


def check_control_permissions() -> PermissionCheck:
    import pyautogui

    make_dpi_aware()
    pyautogui.FAILSAFE = config.PYAUTOGUI_FAILSAFE
    pyautogui.PAUSE = config.PYAUTOGUI_PAUSE_S

    physical = physical_screen_size()
    scale = dpi_scale_factor()
    elevated = False
    if sys.platform == "win32":
        try:
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            elevated = False

    # Trivial mouse move: proves input injection works (macOS Accessibility,
    # Windows UIPI, Wayland restrictions all show up here).
    try:
        x0, y0 = pyautogui.position()
        dx = 3 if x0 < physical[0] - 5 else -3
        pyautogui.moveRel(dx, 0, duration=0)
        x1, y1 = pyautogui.position()
        pyautogui.moveRel(-dx, 0, duration=0)
        moved = (x1, y1) != (x0, y0)
    except Exception as e:
        moved = False
        err = repr(e)
    else:
        err = ""

    # Screenshot must work too (macOS Screen Recording permission).
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        shot_ok = img.size[0] > 0
    except Exception as e:
        shot_ok = False
        err = err or repr(e)

    if moved and shot_ok:
        msg = (f"desktop control OK: {physical[0]}x{physical[1]} physical px, DPI scale {scale:.2f}, "
               f"elevated={elevated}")
        if sys.platform == "win32" and not elevated:
            msg += " (not elevated: windows running as Administrator cannot be controlled)"
        return PermissionCheck(True, msg, physical, scale, elevated)

    if sys.platform == "darwin":
        fix = ("Grant Accessibility AND Screen Recording to your terminal app in "
               "System Settings > Privacy & Security, then restart the terminal.")
    elif sys.platform == "win32":
        fix = ("Input was blocked. If the foreground window is elevated (Run as Administrator), "
               "run the engine from an elevated session too. Also check no remote-desktop/kiosk "
               "policy blocks synthetic input.")
    else:
        fix = "On Wayland, pyautogui cannot inject input; use an X11 session."
    return PermissionCheck(False, f"desktop control BLOCKED ({err or 'mouse did not move / screenshot failed'}). {fix}",
                           physical, scale, elevated)
