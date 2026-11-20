"""The "Recipe running - press Esc to abort" banner shown during a FOREGROUND
replay (the one that needs the user's screen and input).

A small topmost tkinter window on its own thread. WS_EX_NOACTIVATE keeps it from
ever taking focus (the replay's keystrokes must land in the app, not the banner)
and WS_EX_TOOLWINDOW keeps it off the taskbar. The banner is a courtesy, not a
gate: any failure here is logged and the replay proceeds without it.
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
from typing import Optional

from recipe_engine.config import log

BANNER_TEXT = "Recipe running — press Esc to abort"
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


class Banner:
    """show(text) / hide() / close() from any thread; tk lives on its own thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[str, Optional[str]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.hwnd = 0
        self.visible = False
        self.failed = ""

    def _ensure(self) -> bool:
        if self._thread is None or not self._thread.is_alive():
            self._ready.clear()
            self._thread = threading.Thread(target=self._loop, name="overlay-banner", daemon=True)
            self._thread.start()
            self._ready.wait(3.0)
        return not self.failed

    def show(self, text: str = BANNER_TEXT) -> None:
        if self._ensure():
            self._q.put(("show", text))

    def hide(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._q.put(("hide", None))

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._q.put(("close", None))
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------ tk thread
    def _loop(self) -> None:
        try:
            import tkinter as tk
        except Exception as e:
            self.failed = f"tkinter unavailable: {e!r}"
            self._ready.set()
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.92)
            root.configure(bg="#1f1f1f")
            label = tk.Label(root, text=BANNER_TEXT, fg="#ffffff", bg="#1f1f1f", font=("Segoe UI", 12, "bold"), padx=18, pady=8)
            label.pack()
            root.update_idletasks()
            w, h = label.winfo_reqwidth(), label.winfo_reqheight()
            sw = root.winfo_screenwidth()
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+8")
            if sys.platform == "win32":
                root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                self.hwnd = int(hwnd)
                u = ctypes.windll.user32
                ex = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
                u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
        except Exception as e:
            self.failed = f"banner init failed: {e!r}"
            self._ready.set()
            return
        self._ready.set()

        def pump():
            try:
                while True:
                    cmd, arg = self._q.get_nowait()
                    if cmd == "show":
                        label.configure(text=arg or BANNER_TEXT)
                        root.update_idletasks()
                        w2, h2 = label.winfo_reqwidth(), label.winfo_reqheight()
                        root.geometry(f"{w2}x{h2}+{(root.winfo_screenwidth() - w2) // 2}+8")
                        root.deiconify()
                        if sys.platform == "win32":
                            # SWP_NOACTIVATE | SWP_SHOWWINDOW: on top, never focused
                            ctypes.windll.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0010 | 0x0040 | 0x0001 | 0x0002)
                        self.visible = True
                    elif cmd == "hide":
                        root.withdraw()
                        self.visible = False
                    elif cmd == "close":
                        root.destroy()
                        return
            except queue.Empty:
                pass
            except Exception as e:
                log("OVERLAY", f"banner error: {e!r}")
            root.after(50, pump)

        root.after(50, pump)
        try:
            root.mainloop()
        except Exception as e:
            log("OVERLAY", f"banner loop ended: {e!r}")
        self.visible = False


_banner: Optional[Banner] = None
_lock = threading.Lock()


def banner() -> Banner:
    global _banner
    with _lock:
        if _banner is None:
            _banner = Banner()
        return _banner
