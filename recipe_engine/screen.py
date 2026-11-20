"""Screenshots and screenshot-space coordinate mapping.

Claude sees a downscaled JPEG (config.SCREENSHOT_MAX_WIDTH) and passes coordinates in
THAT space; `to_physical()` maps them back through the downscale and the DPI scale
to the logical points pyautogui wants. `_last_shot_size` is the state that makes the
mapping right: a click before any screenshot falls back to the physical size.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

from recipe_engine import config
from recipe_engine.display import dpi_scale_factor, make_dpi_aware, physical_screen_size

_last_shot_size: tuple[int, int] = (0, 0)


@dataclass
class ActionResult:
    """What one desktop action hands back: a note, a fresh screenshot, and an error if
    the action itself did not happen. Tool layers turn this into MCP content."""
    note: str
    jpeg: bytes = b""
    size: tuple[int, int] = (0, 0)
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def screenshot_jpeg() -> tuple[bytes, tuple[int, int]]:
    """Grab the primary screen, downscale, JPEG-encode. Returns (bytes, (w, h))."""
    global _last_shot_size
    from PIL import ImageGrab

    make_dpi_aware()
    img = ImageGrab.grab()
    pw, ph = img.size
    if pw > config.SCREENSHOT_MAX_WIDTH:
        ratio = config.SCREENSHOT_MAX_WIDTH / pw
        img = img.resize((config.SCREENSHOT_MAX_WIDTH, int(ph * ratio)))
    _last_shot_size = img.size
    buf = io.BytesIO()
    # JPEG keeps each screenshot small enough to travel inside a JSON tool result
    # (a full-desktop PNG at 1280 px wide can exceed 1 MB base64).
    img.convert("RGB").save(buf, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), img.size


def shot_size() -> tuple[int, int]:
    return _last_shot_size if _last_shot_size != (0, 0) else physical_screen_size()


def to_physical(x: float, y: float) -> tuple[int, int]:
    """Map screenshot-space coords -> physical pixels -> pyautogui (logical) coords."""
    pw, ph = physical_screen_size()
    sw, sh = _last_shot_size if _last_shot_size != (0, 0) else (pw, ph)
    px = x * pw / sw
    py = y * ph / sh
    scale = dpi_scale_factor()
    return int(round(px / scale)), int(round(py / scale))


def to_screen_physical(x: float, y: float) -> tuple[int, int]:
    """Screenshot-space coords -> PHYSICAL screen pixels (what UI Automation speaks)."""
    pw, ph = physical_screen_size()
    sw, sh = _last_shot_size if _last_shot_size != (0, 0) else (pw, ph)
    return int(round(x * pw / sw)), int(round(y * ph / sh))


def settle_and_shoot(note: str) -> ActionResult:
    """Let the UI settle after an action, then screenshot. The settle is a fixed pause
    on purpose: this is the LIVE (Claude-driven) path, where the next thing that
    happens is Claude looking at the picture. Replay never uses it - replay waits on
    conditions (window title, element visible), never on time."""
    time.sleep(config.ACTION_SETTLE_MS / 1000.0)
    jpeg, size = screenshot_jpeg()
    return ActionResult(note=note, jpeg=jpeg, size=size)
