"""Resolution strategy and DPI math, across the three displays the spec names.

Real multi-DPI is NOT reproducible on a CI runner (one fixed display), so DPI coverage
here is synthetic stamps + arithmetic; the live UIA path is exercised by the
@integration tests against Notepad."""
from __future__ import annotations

import pytest

from recipe_engine import display, resolver, uia

D1080 = {"w": 1920, "h": 1080, "scale": 1.0}
D1440 = {"w": 2560, "h": 1440, "scale": 1.25}
D4K = {"w": 3840, "h": 2160, "scale": 1.5}
DISPLAYS = [D1080, D1440, D4K]

UIA = {"automation_id": "TextEditor", "name": "Text editor", "control_type": "EditControl", "class_name": "Edit",
       "ancestors": [{"control_type": "WindowControl", "name": "Untitled - Notepad", "automation_id": "", "class_name": "Notepad"}],
       "pattern": "set_value", "is_password": False}


def step(uia=None, pixel_display=None, rel=(0.5, 0.5)):
    return {"action": "click", "uia": uia, "pixel": {"rel": list(rel), "display": pixel_display} if pixel_display else None}


@pytest.mark.parametrize("recorded", DISPLAYS)
@pytest.mark.parametrize("now", DISPLAYS)
def test_uia_wins_whenever_it_resolves_regardless_of_display(recorded, now):
    assert resolver.choose_strategy(step(UIA, recorded), now, uia_found=True) == "uia"


@pytest.mark.parametrize("recorded", DISPLAYS)
@pytest.mark.parametrize("now", DISPLAYS)
def test_pixel_only_on_an_identical_display(recorded, now):
    got = resolver.choose_strategy(step(UIA, recorded), now, uia_found=False)
    assert got == ("pixel" if recorded == now else "none")


def test_no_uia_and_no_pixel_is_none():
    assert resolver.choose_strategy({"action": "click", "uia": None, "pixel": None}, D1080, uia_found=False) == "none"
    assert resolver.choose_strategy(step(None, None), D1080, uia_found=True) == "none", "uia_found without a target is meaningless"


@pytest.mark.parametrize("now, want", [(D1080, (960, 540)), (D1440, (1024, 576)), (D4K, (1280, 720))])
def test_rel_to_logical_divides_by_the_dpi_scale(now, want):
    assert display.rel_to_logical((0.5, 0.5), now) == want


def test_display_matches_is_exact_on_all_three_fields():
    assert display.display_matches(D1080, dict(D1080))
    assert not display.display_matches(D1080, D1440)
    assert not display.display_matches(D1080, {**D1080, "scale": 1.25})
    assert not display.display_matches(None, D1080)


def test_resolve_target_refuses_pixels_on_a_different_display(monkeypatch):
    monkeypatch.setattr(uia, "resolve", lambda t, timeout_ms=None, cancel=None: None)
    monkeypatch.setattr(uia, "window_present", lambda t, timeout_ms=1500: True)
    with pytest.raises(resolver.UnresolvableElement) as ei:
        resolver.resolve_target(step(UIA, D1080), D4K, 10)
    assert "unresolvable_element" in str(ei.value) and "pixel fallback refused" in str(ei.value)


def test_resolve_target_uses_pixels_on_the_same_display(monkeypatch):
    monkeypatch.setattr(uia, "resolve", lambda t, timeout_ms=None, cancel=None: None)
    monkeypatch.setattr(uia, "window_present", lambda t, timeout_ms=1500: True)
    r = resolver.resolve_target(step(UIA, D1440, rel=(0.25, 0.5)), D1440, 10)
    assert r.kind == "pixel" and r.xy == (512, 576)


def test_resolve_target_prefers_uia_and_ignores_pixels(monkeypatch):
    class _El:
        BoundingRectangle = (100, 200, 300, 400)    # physical px
    monkeypatch.setattr(uia, "resolve", lambda t, timeout_ms=None, cancel=None: _El())
    monkeypatch.setattr(uia, "window_present", lambda t, timeout_ms=1500: True)
    r = resolver.resolve_target(step(UIA, D1080, rel=(0.9, 0.9)), D4K, 10)
    assert r.kind == "uia" and r.xy == (int(round(200 / 1.5)), int(round(300 / 1.5))) and r.how.startswith("uia:")


def test_uia_target_round_trips_and_labels():
    t = uia.UiaTarget.from_dict(UIA)
    assert t.to_dict() == UIA and t.label == "TextEditor" and t.window["class_name"] == "Notepad"
    assert uia.UiaTarget.from_dict({"name": "OK"}).label == "OK"


def test_title_tail_match_rule():
    assert uia._title_matches("Untitled - Notepad", "*hello.txt - Notepad")
    assert uia._title_matches("Notepad", "report.txt - Notepad")
    assert not uia._title_matches("Untitled - Notepad", "Calculator")


class _FakeControl:
    """Just enough of uiautomation.Control for describe()/_chain_matches()."""

    def __init__(self, ctype, name="", aid="", cls="", parent=None, password=False):
        self.ControlTypeName, self.Name, self.AutomationId, self.ClassName = ctype, name, aid, cls
        self._parent, self.IsPassword = parent, password

    def GetParentControl(self):
        return self._parent

    def GetPattern(self, pid):
        return None


def test_describe_records_the_ancestor_chain_top_down(monkeypatch):
    root = _FakeControl("PaneControl", "Desktop 1", cls="#32769")
    win = _FakeControl("WindowControl", "Untitled - Notepad", cls="Notepad", parent=root)
    pane = _FakeControl("PaneControl", "", cls="NotepadTextBox", parent=win)
    edit = _FakeControl("EditControl", "Text editor", aid="15", cls="Edit", parent=pane, password=False)

    class _Auto:
        class PatternId:
            pass

        class PropertyId:
            IsPasswordProperty = 30019
    monkeypatch.setattr(uia, "_auto", lambda: _Auto)
    t = uia.describe(edit)
    assert t.control_type == "EditControl" and t.automation_id == "15" and t.pattern == "none"
    assert [a["control_type"] for a in t.ancestors] == ["WindowControl", "PaneControl"], "top-level window first, root excluded"
    assert t.window["class_name"] == "Notepad"
    assert uia._chain_matches(edit, t)
    other = _FakeControl("EditControl", "Text editor", parent=_FakeControl("WindowControl", "Calc", parent=root))
    assert not uia._chain_matches(other, uia.UiaTarget(ancestors=[{"control_type": "WindowControl"}, {"control_type": "PaneControl"}]))
