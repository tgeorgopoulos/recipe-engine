"""The desktop actions Claude drives the host with, as plain functions.

These are the bodies of the old executor's `computer` tools with the SDK decorator
and the LLM loop removed. Two things about them are load-bearing:

* `type_text` calls `safety.guard_text` BEFORE any key is pressed. That is the
  credential hook - it used to be a PreToolUse hook in the SDK; now it is the first
  line of the function, where no caller can skip it.
* `type_text` never writes a raw "\\n": pyautogui types Enter for it, which SENDS a
  chat message / submits a form mid-text. Newlines become shift+enter here so a whole
  multi-line message is one action.

Every action records a trace step through `_recorder_hook` (set by the recorder while
a recipe is being recorded) in exactly the shape recipes.build() consumes:
{"surface":"desktop","action","value","post":{"window"},"t","rel":[fx,fy]}.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from recipe_engine import config, safety
from recipe_engine.config import log
from recipe_engine.display import foreground_window_title
from recipe_engine.screen import ActionResult, screenshot_jpeg, settle_and_shoot, shot_size, to_physical

# Set while a real desktop action is executing. Whoever wants to interrupt a replay
# waits for it to clear, so no action is cut in half. Module-global on purpose: the
# desktop is strictly serial (engine.desktop_lock), so at most one action is ever in
# flight; the browser tools share it too.
_action_busy = threading.Event()

# The recorder installs itself here while recording; None otherwise.
_recorder_hook: Optional[Callable[[dict], None]] = None


def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = config.PYAUTOGUI_FAILSAFE
    pyautogui.PAUSE = config.PYAUTOGUI_PAUSE_S
    return pyautogui


def _rec(action: str, args: dict[str, Any], value: Any = None) -> None:
    """Record the step with coordinates as FRACTIONS of the screenshot (resolution-independent)."""
    hook = _recorder_hook
    if hook is None:
        return
    sw, sh = shot_size()
    step: dict[str, Any] = {"surface": "desktop", "action": action, "value": value,
                            "post": {"window": foreground_window_title()}, "t": time.time()}
    if "x" in args and "y" in args and sw and sh:
        step["rel"] = [round(float(args["x"]) / sw, 4), round(float(args["y"]) / sh, 4)]
    # The portable half of the step: WHAT was acted on, by UI Automation identity. The
    # `uia` target is captured BEFORE the hook so a recorder bug cannot lose it, and
    # its failure never fails the action - a step without a target is pixel-only.
    step["uia"] = _describe_target(action, args)
    try:
        hook(step)
    except Exception as e:                 # a recorder bug must never break the action that already happened
        log("DESKTOP", f"recorder hook failed: {e!r}")


def _describe_target(action: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    """UIA description of the element a click hit / the field a type went into."""
    try:
        from recipe_engine import uia
        from recipe_engine.screen import to_screen_physical
        if action in ("click", "double_click", "right_click", "scroll") and "x" in args and "y" in args:
            t = uia.describe_at(*to_screen_physical(float(args["x"]), float(args["y"])))
        elif action == "type":
            t = uia.describe_focused()
            # Only a field inside a real, titled window is worth re-finding at replay. When another
            # process holds the foreground lock, "focus" reports its hidden message window - a
            # target that would cost a full resolve timeout for nothing.
            if t is not None and (t.window is None or not (t.window.get("name") or "").strip() or t.window.get("class_name") == "Message"):
                return None
        else:
            return None
        return t.to_dict() if t is not None else None
    except Exception as e:
        log("DESKTOP", f"uia describe skipped: {e!r}")
        return None


def _busy(fn: Callable[[], ActionResult]) -> ActionResult:
    _action_busy.set()
    try:
        return fn()
    finally:
        _action_busy.clear()


# ---------------------------------------------------------------------------- actions
def screenshot() -> ActionResult:
    jpeg, size = screenshot_jpeg()
    hook = _recorder_hook
    if hook is not None:
        hook({"surface": "desktop", "action": "screenshot", "t": time.time()})
    return ActionResult(note=f"Screenshot {size[0]}x{size[1]} (coordinates you pass must be in this space).",
                        jpeg=jpeg, size=size)


def click(x: int, y: int) -> ActionResult:
    def go() -> ActionResult:
        px, py = to_physical(x, y)
        _pyautogui().click(px, py)
        out = settle_and_shoot(f"clicked at ({x}, {y})")
        _rec("click", {"x": x, "y": y})
        return out
    return _busy(go)


def double_click(x: int, y: int) -> ActionResult:
    def go() -> ActionResult:
        px, py = to_physical(x, y)
        _pyautogui().doubleClick(px, py)
        out = settle_and_shoot(f"double-clicked at ({x}, {y})")
        _rec("double_click", {"x": x, "y": y})
        return out
    return _busy(go)


def right_click(x: int, y: int) -> ActionResult:
    def go() -> ActionResult:
        px, py = to_physical(x, y)
        _pyautogui().rightClick(px, py)
        out = settle_and_shoot(f"right-clicked at ({x}, {y})")
        _rec("right_click", {"x": x, "y": y})
        return out
    return _busy(go)


def move(x: int, y: int) -> ActionResult:
    def go() -> ActionResult:
        px, py = to_physical(x, y)
        _pyautogui().moveTo(px, py, duration=0.1)
        out = settle_and_shoot(f"moved to ({x}, {y})")
        _rec("move", {"x": x, "y": y})
        return out
    return _busy(go)


def type_text(text: str) -> ActionResult:
    """Type into the focused field. Raises CredentialRefused before touching the keyboard
    if the text looks like a credential OR the focused control is a password/code field
    (UIA IsPassword, or a name like Password / CVV). Newlines are typed as shift+enter."""
    text = str(text or "")
    if not text:
        return ActionResult(note="", error="nothing to type")
    safety.guard_text(text)

    def go() -> ActionResult:
        safety.guard_focused_element()          # IsPassword / a field named Password, CVV...: nothing is typed
        pg = _pyautogui()
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if seg:
                pg.write(seg, interval=config.TYPE_INTERVAL_S)
            if i < len(segments) - 1:
                pg.hotkey("shift", "enter")
        out = settle_and_shoot(f"typed {len(text)} characters"
                               + (f" on {len(segments)} lines" if len(segments) > 1 else ""))
        _rec("type", {"text": text}, text)
        return out
    return _busy(go)


def key(combo: str) -> ActionResult:
    combo = str(combo or "").strip().lower()
    if not combo:
        return ActionResult(note="", error="empty key combo")

    def go() -> ActionResult:
        pg = _pyautogui()
        parts = [p.strip() for p in combo.replace(" ", "").split("+") if p.strip()]
        if len(parts) == 1:
            pg.press(parts[0])
        else:
            pg.hotkey(*parts)
        out = settle_and_shoot(f"pressed {combo}")
        _rec("key", {"combo": combo}, combo)
        return out
    return _busy(go)


def scroll(x: int, y: int, clicks: int) -> ActionResult:
    def go() -> ActionResult:
        px, py = to_physical(x, y)
        _pyautogui().scroll(int(clicks), x=px, y=py)
        out = settle_and_shoot(f"scrolled {clicks} at ({x}, {y})")
        _rec("scroll", {"x": x, "y": y, "clicks": clicks}, int(clicks))
        return out
    return _busy(go)


def open_browser(url: str) -> ActionResult:
    """Open a URL as a new tab of the engine's own persistent Chrome profile (the
    profile has exactly ONE owner: the Playwright context)."""
    url = config.normalize_url(str(url or ""))

    def go() -> ActionResult:
        try:
            from recipe_engine import browser
            log("BROWSER", f"opening {url} on the engine profile {config.BROWSER_PROFILE_DIR}")
            browser.open_url(url)
        except Exception as e:
            return ActionResult(note="", error=f"could not open the engine's browser: {e!r}")
        out = settle_and_shoot(f"opened {url} in the engine's Chrome profile")
        _rec("open_browser", {"url": url}, url)
        return out
    return _busy(go)


def wait(seconds: float) -> ActionResult:
    secs = max(0.0, min(10.0, float(seconds if seconds is not None else 1)))
    time.sleep(secs)
    hook = _recorder_hook
    if hook is not None:
        hook({"surface": "desktop", "action": "wait", "value": secs, "t": time.time()})
    return settle_and_shoot(f"waited {secs:.1f}s")


ACTIONS = {"screenshot": screenshot, "click": click, "double_click": double_click, "right_click": right_click,
           "move": move, "type": type_text, "key": key, "scroll": scroll, "open_browser": open_browser, "wait": wait}
