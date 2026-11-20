"""System-tray icon for the host process. Runs on the main thread (pystray needs it
on Windows). Falls back to a plain wait when pystray or a display is unavailable
(CI, a session without a shell), so headless tests can still run `serve --no-tray`."""
from __future__ import annotations

import threading
import webbrowser
from typing import Callable

from recipe_engine import auth
from recipe_engine.config import log


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (4, 6, 12, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 61, 61), radius=12, outline=(25, 230, 255, 255), width=3)
    d.ellipse((18, 18, 46, 46), outline=(25, 230, 255, 255), width=4)
    d.ellipse((27, 27, 37, 37), fill=(255, 43, 214, 255))
    return img


def run(eng, url: str, on_quit: Callable[[], None]) -> None:
    try:
        import pystray
    except Exception as e:
        log("TRAY", f"pystray unavailable ({e!r}); running without a tray icon")
        threading.Event().wait()
        return

    def open_panel(icon, item):
        # The panel authenticates with the same install token, handed over in the fragment.
        webbrowser.open(f"{url}/#t={auth.read_token()}")

    def show_status(icon, item):
        s = eng.status()
        icon.notify(f"invariants {'ok' if s['invariants_ok'] else 'BROKEN'} - "
                    f"{'desktop busy' if s['input_owned'] else 'idle'} - {s['recipes']} recipes", "Recipe Engine")

    def quit_(icon, item):
        icon.visible = False
        icon.stop()
        on_quit()

    icon = pystray.Icon("recipe-engine", _icon_image(), "Recipe Engine",
                        menu=pystray.Menu(pystray.MenuItem("Open panel", open_panel, default=True),
                                          pystray.MenuItem("Status", show_status),
                                          pystray.MenuItem("Quit", quit_)))
    log("TRAY", "tray icon up (Open panel / Status / Quit)")
    try:
        icon.run()
    except Exception as e:
        log("TRAY", f"tray loop failed ({e!r}); running headless")
        threading.Event().wait()
