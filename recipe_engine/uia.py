"""Windows UI Automation: describe the element under a point / with focus at record
time, and find it again at replay time by what it IS, not where it was.

A recorded target is {automation_id, name, control_type, class_name, ancestors[],
pattern, is_password}. `resolve()` walks: the top-level window (class + title tail)
-> AutomationId -> ControlType+Name -> ControlType+ClassName, and only accepts an
UNAMBIGUOUS match (a second candidate with the same properties means "not sure",
and the engine does not guess with the user's mouse). Everything polls through the
library's own bounded Exists(); there is no fixed sleep.

Patterns (Invoke / Value / Toggle / SelectionItem) are provider callbacks that do
not need focus or the foreground - they are what makes background replay possible
for the steps that have them.

Call display.make_dpi_aware() before importing this module in a new process.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from recipe_engine import config
from recipe_engine.config import log

_ROOT_CLASSES = ("#32769",)                 # the desktop root pane
_MAX_ANCESTORS = 12


@dataclass
class UiaTarget:
    automation_id: str = ""
    name: str = ""
    control_type: str = ""                  # e.g. "EditControl", "ButtonControl"
    class_name: str = ""
    ancestors: list[dict] = field(default_factory=list)   # top-level window first, direct parent last
    pattern: str = "none"                   # invoke | set_value | toggle | select | none
    is_password: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UiaTarget":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def window(self) -> Optional[dict]:
        return self.ancestors[0] if self.ancestors else None

    @property
    def label(self) -> str:
        return self.automation_id or self.name or f"{self.control_type}/{self.class_name}"


def _auto():
    import uiautomation as auto
    return auto


def _title_matches(expected: str, actual: str) -> bool:
    """Same rule the window-title assertion uses: case-insensitive, ignore a leading
    '*' (dirty marker), compare the tail after the last ' - ' as a substring."""
    e = (expected or "").strip().lstrip("*").strip().lower()
    a = (actual or "").strip().lstrip("*").strip().lower()
    if not e:
        return True
    et = e.rsplit(" - ", 1)[-1]
    return et in a or e in a


# ---------------------------------------------------------------- describe
def _pattern_of(c) -> str:
    auto = _auto()
    P = auto.PatternId
    for pid, name in ((getattr(P, "InvokePattern", None), "invoke"), (getattr(P, "ValuePattern", None), "set_value"),
                      (getattr(P, "TogglePattern", None), "toggle"), (getattr(P, "SelectionItemPattern", None), "select")):
        if pid is None:
            continue
        try:
            p = c.GetPattern(pid)
        except Exception:
            p = None
        if p:
            if name == "set_value":
                try:
                    if getattr(p, "IsReadOnly", False):
                        continue
                except Exception:
                    pass
            return name
    return "none"


def _is_password(c) -> bool:
    try:
        v = c.IsPassword
        return bool(v() if callable(v) else v)
    except Exception:
        try:
            return bool(c.GetPropertyValue(_auto().PropertyId.IsPasswordProperty))
        except Exception:
            return False


def _brief(c) -> dict[str, str]:
    def g(attr):
        try:
            v = getattr(c, attr)
            return str(v() if callable(v) else v or "")
        except Exception:
            return ""
    return {"control_type": g("ControlTypeName"), "name": g("Name")[:120], "automation_id": g("AutomationId")[:120],
            "class_name": g("ClassName")[:80]}


def _root_handle() -> int:
    try:
        import ctypes
        return int(ctypes.windll.user32.GetDesktopWindow())
    except Exception:
        return 0


def _is_root(ctl, pb: dict[str, str], root_hwnd: int) -> bool:
    if pb["class_name"] in _ROOT_CLASSES:
        return True
    try:
        h = ctl.NativeWindowHandle
        if root_hwnd and int(h() if callable(h) else h or 0) == root_hwnd:
            return True
    except Exception:
        pass
    return pb["control_type"] == "PaneControl" and pb["name"].startswith("Desktop")


def describe(c) -> UiaTarget:
    b = _brief(c)
    chain: list[dict] = []
    cur = c
    root_hwnd = _root_handle()
    for _ in range(_MAX_ANCESTORS):
        try:
            cur = cur.GetParentControl()
        except Exception:
            break
        if cur is None:
            break
        pb = _brief(cur)
        if _is_root(cur, pb, root_hwnd):
            break
        chain.append(pb)
    chain.reverse()                          # top-level window first
    return UiaTarget(automation_id=b["automation_id"], name=b["name"], control_type=b["control_type"],
                     class_name=b["class_name"], ancestors=chain, pattern=_pattern_of(c), is_password=_is_password(c))


def describe_at(px: int, py: int) -> Optional[UiaTarget]:
    """Element under a PHYSICAL screen point. None if UIA has nothing there."""
    try:
        c = _auto().ControlFromPoint(int(px), int(py))
        return describe(c) if c is not None else None
    except Exception as e:
        log("UIA", f"describe_at failed: {e!r}")
        return None


def describe_focused() -> Optional[UiaTarget]:
    try:
        c = _auto().GetFocusedControl()
        return describe(c) if c is not None else None
    except Exception as e:
        log("UIA", f"describe_focused failed: {e!r}")
        return None


# ---------------------------------------------------------------- resolve
def _exists(c, seconds: float, cancel=None) -> bool:
    """Bounded existence poll in 0.2 s slices so an abort (cancel event) is honoured
    within the abort budget even in the middle of a 5 s search."""
    deadline = time.time() + max(0.0, seconds)
    while True:
        if cancel is not None and cancel.is_set():
            return False
        slice_s = min(0.2, max(0.0, deadline - time.time()))
        try:
            found = bool(c.Exists(maxSearchSeconds=slice_s, searchIntervalSeconds=0.1))
        except TypeError:
            found = bool(c.Exists(slice_s, 0.1))
        except Exception:
            return False
        if found or time.time() >= deadline:
            return found


def _top_level_windows() -> list[tuple[int, str, str]]:
    """(hwnd, class, title) of every visible, non-minimized top-level window - on ANY
    virtual desktop. UIA's root only lists the current desktop's windows, so a window
    parked on the background-replay scratch desktop is found here, not there."""
    import ctypes
    import ctypes.wintypes as W
    u = ctypes.windll.user32
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(hwnd, _):
        try:
            if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                return True
            rect = W.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
                return True                               # nothing can be clicked in a 0x0 window
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            out.append((int(hwnd), cls.value, buf.value))
        except Exception:
            pass
        return True
    u.EnumWindows(cb, 0)
    return out


def _find_window(t: UiaTarget, deadline: float, cancel=None):
    auto = _auto()
    win = t.window
    if not win:
        # The target IS a top-level window (no ancestors): find it by its own identity.
        if t.control_type == "WindowControl" and (t.class_name or t.name):
            win = {"class_name": t.class_name, "name": t.name}
        else:
            return auto.GetRootControl()
    cls, title = win.get("class_name") or "", win.get("name") or ""
    want = title.strip().lstrip("*").strip().lower()
    # An EXACT title wins over a tail match, so "Untitled - Notepad" never resolves to the
    # user's own "Budget - Notepad"; a tail match is accepted only when it is the ONLY candidate.
    while True:
        exact, loose = None, []
        for hwnd, wcls, name in _top_level_windows():
            if cls and wcls != cls:
                continue
            if not title:
                loose.append(hwnd)
            elif name.strip().lstrip("*").strip().lower() == want:
                exact = exact or hwnd
            elif _title_matches(title, name):
                loose.append(hwnd)
        pick = exact if exact is not None else (loose[0] if len(loose) == 1 else None)
        if pick is not None:
            try:
                return auto.ControlFromHandle(pick)
            except Exception as e:
                log("UIA", f"ControlFromHandle({pick:#x}) failed: {e!r}")
                return None
        if len(loose) > 1:
            log("UIA", f"window {title!r}: {len(loose)} windows match by title tail and none exactly - not guessing")
            return None
        if time.time() >= deadline or (cancel is not None and cancel.is_set()):
            return None
        time.sleep(0.1)


def window_present(t: UiaTarget, timeout_ms: int = 1500) -> bool:
    """Is the recorded top-level window (class + title) on screen right now?"""
    return _find_window(t, time.time() + timeout_ms / 1000.0) is not None


def _unique(make, deadline: float, cancel=None):
    """make(foundIndex) -> Control. Returns the first match only if there is no second."""
    first = make(1)
    if not _exists(first, deadline - time.time(), cancel):
        return None
    second = make(2)
    if _exists(second, 0.0):
        return "ambiguous"
    return first


def resolve(t: UiaTarget, timeout_ms: Optional[int] = None, cancel=None):
    """The recorded element, or None. Bounded by timeout_ms (config default); an
    abort (`cancel` set) ends the search at its next 0.2 s slice."""
    auto = _auto()
    deadline = time.time() + (timeout_ms or config.UIA_RESOLVE_TIMEOUT_MS) / 1000.0
    win = _find_window(t, deadline, cancel)
    if win is None:
        log("UIA", f"window not found for {t.label!r}: {t.window}")
        return None
    if not t.ancestors and t.control_type == "WindowControl":
        return win
    ctype = getattr(auto.ControlType, t.control_type, None) if t.control_type else None

    def by(**props):
        return lambda idx: auto.Control(searchFromControl=win, foundIndex=idx, **{k: v for k, v in props.items() if v})

    attempts = []
    if t.automation_id:
        attempts.append(("automation_id", by(AutomationId=t.automation_id)))
        attempts.append(("automation_id+type", by(AutomationId=t.automation_id, ControlType=ctype)))
    if t.name:
        attempts.append(("type+name", by(ControlType=ctype, Name=t.name)))
    if t.class_name:
        attempts.append(("type+class", by(ControlType=ctype, ClassName=t.class_name)))
    for how, make in attempts:
        if time.time() > deadline or (cancel is not None and cancel.is_set()):
            break
        found = _unique(make, deadline, cancel)
        if found == "ambiguous":
            # disambiguate by the recorded ancestor chain
            for idx in range(1, 6):
                cand = make(idx)
                if not _exists(cand, 0.0):
                    break
                if _chain_matches(cand, t):
                    return cand
            continue
        if found is not None:
            return found
    return None


def _chain_matches(c, t: UiaTarget) -> bool:
    got = describe(c).ancestors
    want = t.ancestors
    n = min(len(got), len(want))
    if n == 0:
        return False
    return all(g.get("control_type") == w.get("control_type") for g, w in zip(got[-n:], want[-n:]))


# ---------------------------------------------------------------- act
def element_center_logical(c, display_now: Optional[dict] = None) -> tuple[int, int]:
    """Centre of the element's bounding rect (PHYSICAL px from UIA) as pyautogui logical coords."""
    from recipe_engine.display import current_display
    now = display_now or current_display()
    r = c.BoundingRectangle
    try:
        left, top, right, bottom = r.left, r.top, r.right, r.bottom
    except AttributeError:
        left, top, right, bottom = r
    scale = float(now.get("scale") or 1.0)
    return int(round((left + right) / 2 / scale)), int(round((top + bottom) / 2 / scale))


class PatternUnavailable(Exception):
    """The element does not expose the pattern the step needs (background mode never
    falls back to real input mid-run: that would steal the user's screen)."""


def perform_pattern(c, pattern: str, value: Optional[str] = None) -> None:
    """Drive `c` by a UIA pattern - no focus, no foreground, no real input."""
    auto = _auto()
    P = auto.PatternId
    try:
        if pattern == "invoke":
            # The MSAA bridge's default action is the same press, and some providers (Store
            # Calculator) take 2.5 s to act on InvokePattern.Invoke but 0.5 s on it.
            legacy = c.GetPattern(getattr(P, "LegacyIAccessiblePattern", 0))
            if legacy:
                legacy.DoDefaultAction()
                return
            p = c.GetPattern(P.InvokePattern)
            if not p:
                raise PatternUnavailable("invoke")
            p.Invoke()
        elif pattern == "toggle":
            p = c.GetPattern(P.TogglePattern)
            if not p:
                raise PatternUnavailable("toggle")
            p.Toggle()
        elif pattern == "select":
            p = c.GetPattern(P.SelectionItemPattern)
            if not p:
                raise PatternUnavailable("select")
            p.Select()
        elif pattern == "set_value":
            p = c.GetPattern(P.ValuePattern)
            if not p:
                raise PatternUnavailable("set_value")
            p.SetValue(str(value if value is not None else ""))
        else:
            raise PatternUnavailable(pattern or "none")
    except PatternUnavailable:
        raise
    except Exception as e:
        raise PatternUnavailable(f"{pattern}: {type(e).__name__}: {str(e)[:120]}") from None


def invoke(c) -> None:
    c.GetPattern(_auto().PatternId.InvokePattern).Invoke()


def set_value(c, text: str) -> None:
    c.GetPattern(_auto().PatternId.ValuePattern).SetValue(text)


def toggle(c) -> None:
    c.GetPattern(_auto().PatternId.TogglePattern).Toggle()


def select(c) -> None:
    c.GetPattern(_auto().PatternId.SelectionItemPattern).Select()


def self_test() -> dict[str, Any]:
    """Prove UIA answers at all: the taskbar or the desktop root must describe."""
    try:
        auto = _auto()
        root = auto.GetRootControl()
        d = describe(root)
        tray = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
        ok = _exists(tray, 1.0)
        return {"uia_ok": True, "root": d.control_type, "taskbar": ok}
    except Exception as e:
        return {"uia_ok": False, "error": repr(e)}
