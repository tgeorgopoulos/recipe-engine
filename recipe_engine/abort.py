"""Global Esc abort for a running replay, and the key release that goes with it.

Two mechanisms, because each one alone has a hole:
  * a watchdog thread polls GetAsyncKeyState(VK_ESCAPE) every ESC_POLL_MS (50 ms):
    needs no registration, works whatever window is in front, cannot be silently
    unhooked the way a low-level keyboard hook can when the process is slow;
  * RegisterHotKey(Esc) on the same thread, best effort: while a replay runs it
    swallows the Esc so the app being driven does not also see a stray Escape.
    If registration fails (another app owns it) the poll still catches the key.

fire() sets the replay's cancel event AND releases every key the replay may be
holding (modifiers first, then anything GetAsyncKeyState says is down, then the
mouse buttons) so an aborted "ctrl+shift+..." never leaves the user with a stuck
modifier. The budget is config.ABORT_DEADLINE_MS (200 ms) from key-down to fire.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Callable, Optional

from recipe_engine import config
from recipe_engine.config import log

VK_ESCAPE = 0x1B
# the modifier virtual keys a replay could be holding: generic + left/right variants + Win keys
MODIFIER_VKS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEUP = 0x0040
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
_HOTKEY_ID = 0x5EC


if sys.platform == "win32":
    import ctypes.wintypes as W

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


def _key_down(vk: int) -> bool:
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _send(inputs: list) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return int(ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(INPUT)))


def _keyup(vk: int, flags: int = KEYEVENTF_KEYUP):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    return i


def _mouseup(flag: int):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=None)
    return i


def release_all_keys(send: Optional[Callable[[list], int]] = None, key_down: Optional[Callable[[int], bool]] = None) -> list[str]:
    """KEYUP every modifier, every other key reported down, and all three mouse
    buttons. Returns what was released (for the log and the tests). `send` and
    `key_down` are injectable so the unit test never touches the real input queue."""
    if sys.platform != "win32":
        return []
    send = send or _send
    key_down = key_down or _key_down
    released: list[str] = []
    batch = []
    for vk in MODIFIER_VKS:
        batch.append(_keyup(vk, KEYEVENTF_KEYUP))
        released.append(f"vk{vk:02X}")
    for vk in range(0x08, 0xFF):
        if vk in MODIFIER_VKS or vk in (0x01, 0x02, 0x04):
            continue
        if key_down(vk):
            batch.append(_keyup(vk, KEYEVENTF_KEYUP))
            released.append(f"vk{vk:02X}")
    for flag, name in ((MOUSEEVENTF_LEFTUP, "mouse-left"), (MOUSEEVENTF_RIGHTUP, "mouse-right"), (MOUSEEVENTF_MIDDLEUP, "mouse-middle")):
        batch.append(_mouseup(flag))
        released.append(name)
    try:
        send(batch)
    except Exception as e:
        log("ABORT", f"release_all_keys: SendInput failed: {e!r}")
    return released


class AbortWatch:
    """Watches for Esc while a replay runs. `on_abort()` is called from the watch
    thread at most once; the caller's cancel event is what the replay loop checks."""

    def __init__(self, on_abort: Callable[[], None], poll_ms: Optional[int] = None,
                 key_down: Optional[Callable[[int], bool]] = None, hotkey: bool = True) -> None:
        self.on_abort = on_abort
        self.poll_s = (poll_ms or config.ESC_POLL_MS) / 1000.0
        self._key_down = key_down or _key_down
        self._hotkey = hotkey and sys.platform == "win32"
        self.fired = threading.Event()
        self.fired_at: Optional[float] = None
        self.reason = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.hotkey_registered = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "AbortWatch":
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="esc-abort-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)

    def __enter__(self) -> "AbortWatch":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------ firing
    def fire(self, reason: str = "esc") -> None:
        if self.fired.is_set():
            return
        self.fired_at = time.time()
        self.reason = reason
        self.fired.set()
        log("ABORT", f"abort ({reason}): releasing keys and stopping the replay at its next check")
        release_all_keys()
        try:
            self.on_abort()
        except Exception as e:
            log("ABORT", f"on_abort raised: {e!r}")

    def _run(self) -> None:
        u = ctypes.windll.user32 if sys.platform == "win32" else None
        if self._hotkey and u is not None:
            try:
                self.hotkey_registered = bool(u.RegisterHotKey(None, _HOTKEY_ID, MOD_NOREPEAT, VK_ESCAPE))
            except Exception:
                self.hotkey_registered = False
        # an Esc that is ALREADY down when the watch starts is not an abort request
        armed = not self._key_down(VK_ESCAPE)
        try:
            while not self._stop.is_set():
                if self.hotkey_registered:
                    msg = W.MSG()
                    while u.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, 1):
                        if msg.wParam == _HOTKEY_ID:
                            self.fire("esc hotkey")
                            return
                down = self._key_down(VK_ESCAPE)
                if down and armed:
                    self.fire("esc")
                    return
                if not down:
                    armed = True
                self._stop.wait(self.poll_s)
        finally:
            if self.hotkey_registered and u is not None:
                try:
                    u.UnregisterHotKey(None, _HOTKEY_ID)
                except Exception:
                    pass
                self.hotkey_registered = False
