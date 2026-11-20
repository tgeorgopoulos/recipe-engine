"""Where does a recorded desktop step's target live on THIS screen, right now?

Resolution order, in one place so the invariant self-test can pin it:

  1. UIA first. The step recorded the element's AutomationId / Name / ControlType /
     ClassName and its ancestor chain. If that resolves to exactly one element, we use
     it - and pixel coordinates are ignored even if present. This is what makes a
     recipe survive a moved window, a different resolution, a different DPI.
  2. Pixel fallback ONLY if UIA fails AND the recorded display stamp (w, h, scale)
     matches the current display exactly. Fractional coordinates on a different
     screen are a guess, and the engine does not guess with the user's mouse.
  3. Otherwise the step fails with reason "unresolvable_element".

`choose_strategy` is pure (unit-tested across display stamps); `resolve_target`
does the I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from recipe_engine import config
from recipe_engine.config import log
from recipe_engine.display import current_display, display_matches, rel_to_logical

Strategy = Literal["uia", "pixel", "none"]


class UnresolvableElement(Exception):
    reason = "unresolvable_element"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)
        self.detail = detail


@dataclass
class Resolved:
    kind: Literal["uia", "pixel"]
    element: Any = None                 # a uia.Control when kind == "uia"
    xy: Optional[tuple[int, int]] = None  # logical coords for pyautogui
    how: str = ""


def choose_strategy(step: dict[str, Any], display_now: dict[str, Any], uia_found: bool) -> Strategy:
    """Pure decision. `uia_found` is whether the recorded UIA target resolved."""
    if step.get("uia") and uia_found:
        return "uia"
    pixel = step.get("pixel") or {}
    if pixel.get("rel") and display_matches(pixel.get("display"), display_now):
        return "pixel"
    return "none"


def resolve_target(step: dict[str, Any], display_now: Optional[dict[str, Any]] = None,
                   timeout_ms: Optional[int] = None, pixel_ok: bool = True,
                   cancel: Optional[Any] = None) -> Resolved:
    """UIA first; pixel only on a matching display (and only when `pixel_ok`, which
    background replay turns off - there is no screen to click on); else UnresolvableElement."""
    display_now = display_now or current_display()
    timeout_ms = int(timeout_ms or config.UIA_RESOLVE_TIMEOUT_MS)
    element = None
    why = ""
    if step.get("uia"):
        try:
            from recipe_engine import uia
            element = uia.resolve(uia.UiaTarget.from_dict(step["uia"]), timeout_ms, cancel=cancel)
            if element is None:
                why = "no unique UIA match"
        except Exception as e:                       # the UIA layer must never take a replay down uncontrolled
            why = f"uia error: {type(e).__name__}: {str(e)[:120]}"
            element = None
    if element is None and step.get("uia") and (step["uia"].get("ancestors") or step["uia"].get("control_type") == "WindowControl"):
        from recipe_engine import uia
        if not uia.window_present(uia.UiaTarget.from_dict(step["uia"])):
            # the app window itself is gone: a pixel click would land on whatever is on screen now
            raise UnresolvableElement(f"window not found: {(step['uia'].get('ancestors') or [{}])[0].get('name') or step['uia'].get('name')!r} "
                                      f"(class {(step['uia'].get('ancestors') or [{}])[0].get('class_name') or step['uia'].get('class_name')!r}); "
                                      f"no pixel fallback without the window")
    strategy = choose_strategy(step, display_now, element is not None)
    if strategy == "uia":
        from recipe_engine import uia
        xy = uia.element_center_logical(element, display_now)
        return Resolved("uia", element=element, xy=xy, how="uia:" + (step["uia"].get("automation_id") or step["uia"].get("name") or step["uia"].get("control_type") or "?"))
    if pixel_ok and step.get("pixel") and display_matches(step["pixel"].get("display"), display_now):
        xy = rel_to_logical(step["pixel"]["rel"], display_now)
        log("RESOLVE", f"pixel fallback at {xy} ({why or 'no uia target recorded'})")
        return Resolved("pixel", xy=xy, how="pixel")
    detail = why or ("no uia target recorded" if not step.get("uia") else "")
    if not pixel_ok and step.get("pixel"):
        detail += "; pixel fallback not allowed in background mode"
    if step.get("pixel") and not display_matches(step["pixel"].get("display"), display_now):
        detail += f"; pixel fallback refused: recorded display {step['pixel'].get('display')} != current {display_now}"
    raise UnresolvableElement(detail.strip("; "))
